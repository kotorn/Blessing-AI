# Blessing AI — UI Data Contract Gap Report
**Reference Document:** `UI-UX-PLAN.md`  
**Classification Rules:**
- `EXISTING`: Backend already provides authoritative data field in `/api/quant/state`, `/api/binance/*`, or `/api/bigquery/*`.
- `DERIVED_FRONTEND`: Field is calculated deterministically on the frontend from existing backend data without fabricating numbers.
- `PROPOSED_BACKEND`: Required for full trading OS traceability; pending backend contract implementation. Must show "Contract Pending" or be hidden until backed.
- `RESEARCH_ONLY`: Available only in offline backtest or BigQuery lakehouse; not in live state.

---

## 1. Global & System Health

| Field / Model | Status | Current Source / Notes | Temporary UI Behavior |
|---|---|---|---|
| `SystemMode` (`PAPER`, `TESTNET`, `LIVE`) | `EXISTING` | Inferred from `AccountData.source` (`BINANCE_LIVE`, `BINANCE_TESTNET`, `SIMULATED`) | Rendered in `StatusBar` mode pill |
| `ServiceHealth` (Worker heartbeat, DB latency, WS status) | `PROPOSED_BACKEND` | Only `/api/health` returns `{status: "ok"}` currently | Display basic HTTP status; mark detailed worker heartbeat as pending |
| `PauseNewRisk` / `RecoveryOnly` system-wide endpoints | `PROPOSED_BACKEND` | Currently only per-basket recovery (`/api/quant/basket/recovery`) and kill-switch exist | Show control with disabled/confirmation or pending backend hook |
| `KillSwitch` | `EXISTING` | `POST /api/quant/risk/kill-switch` and `POST /api/quant/killswitch` | Functional in StatusBar |

---

## 2. Market State & Microstructure

| Field / Model | Status | Current Source / Notes | Temporary UI Behavior |
|---|---|---|---|
| `InstrumentData.regime` | `EXISTING` | `InstrumentData.regime` in `/api/quant/state` | Rendered on Market State cards |
| `regime_probabilities` | `EXISTING` | Available in `/api/quant/state` | Displayed on Market State cards |
| `Spot/Perp Basis & Funding Carry` | `EXISTING` | Spot price, Perp mark, Basis z-score, 8h funding rate in `/api/quant/state` | Rendered in `BasisFundingCarryMonitor` with post-fee carry modeling |
| `Multi-Horizon Shock Velocity/Acceleration` | `DERIVED_FRONTEND` | Calculated across 4 horizons (5s, 15s, 1m, 5m) normalized against rolling volatility distribution (sigma_roll) | Rendered in `MultiHorizonShockMonitor` |
| `Measurable Structural Levels` | `DERIVED_FRONTEND` | 24h swing high/low, equilibrium pivot, liquidity sweep cluster | Rendered in `MarketStructureCard` with Acceptance/Rejection badges |
| `spreadBps`, `bookImbalance`, `CVD` | `EXISTING` | Hooked up to real-time Binance WebSocket (`@depth10` and `@aggTrade`) in the browser. | Rendered in `MicrostructureMonitor` |

---

## 3. Strategy Intents & Opportunity Scores

| Field / Model | Status | Current Source / Notes | Temporary UI Behavior |
|---|---|---|---|
| `StrategyIntentStream` (per-engine intent feed) | `DERIVED_FRONTEND` | Modeled from active basket models & alpha engine definitions (`structural_grid`, `trend_breakout`, `shock_momentum`, `funding_carry`) | Rendered in `StrategyIntentStream` (UI-05) |
| `OpportunityScore` (0-100 continuous score) | `EXISTING` / `DERIVED_FRONTEND` | `grid_safety_score` authoritative in `/api/quant/state`; trend/shock/carry scores mapped continuously | Rendered on intent cards |
| `MetaAllocatorMatrix` (continuous risk units) | `DERIVED_FRONTEND` | Continuous multiplier factors (0.0x–2.0x) with common crypto beta discounts | Rendered in `MetaAllocationMatrix` (UI-05) |
| `ConflictResolution` (Target Exposure vs Physical Orders) | `DERIVED_FRONTEND` | Implements PLAN.md netting invariant (e.g. +1.0 BTC grid vs -0.6 trend vs -0.3 shock -> +0.1 BTC net) | Rendered in `StrategyConflictResolver` (UI-05) |
| `ConservatismMetrics` (Veto audit, zero exposure time) | `DERIVED_FRONTEND` | Tracks caution status, lost alpha vs tail-risk reduction | Rendered in `ConservatismControlCard` (UI-05) |
| `Real-Time NATS JetStream Intent Bus` | `PROPOSED_BACKEND` | Live distributed event bus for Python engine intent emission | Documented for future backend sprint |

---

## 4. Target Exposure & Execution Pipeline

| Field / Model | Status | Current Source / Notes | Temporary UI Behavior |
|---|---|---|---|
| `TargetExposureView.strategyAttribution` | `PROPOSED_BACKEND` | No attribution currently in `/api/quant/state` | Show actual exposure; mark intent decomposition pending |
| `OrderView` / Fills Table | `EXISTING` | Dedicated `orders` array retrieved from `/api/quant/state` decoupled from active baskets | Implemented in `OrdersTable` (UI-06A) |
| `ExecutionPipeline` (Correlation IDs) | `EXISTING` | 6-stage deterministic trace (Intent -> Score -> Allocator -> Governor -> Target -> Binance Fill) via standalone order objects | Implemented in `ExecutionTraceViewer` (UI-06C) |
| `Dedicated GET /api/orders` | `PROPOSED_BACKEND` | Optional future backend endpoint for historical pagination (currently batched in state) | Documented for future backend sprint |

---

## 5. Positions & Baskets

| Field / Model | Status | Current Source / Notes | Temporary UI Behavior |
|---|---|---|---|
| `BasketItem` (State, levels, average entry, fees, PnL) | `EXISTING` | Full support in `/api/quant/state` and `BasketManager` | Preserved and rendered in Positions & Baskets |
| `BasketActions` (expand, recovery, close) | `EXISTING` | `/api/quant/basket/expand`, `recovery`, `close` | Preserved with full confirmation |
| `TwoLayerAsset` & `SubWalletSummary` | `EXISTING` | Fully supported via `/api/binance/sync-account` and `AccountData` | Preserved in Portfolio / Balance Allocation |

---

## 6. Portfolio Risk Governor & Capital Preservation (UI-07)

| Field / Model | Status | Current Source / Notes | Temporary UI Behavior |
|---|---|---|---|
| `HardRiskBounds` (Margin, Leverage, Drawdown, Liquidation) | `EXISTING` | Authoritative in `/api/quant/state` (`account.risk_state`, rules array) | Rendered in `HardRiskBoundsCard` with invariant gauges |
| `ExposureRecoveryAssessment` (Reduce vs Counter-Hedge) | `DERIVED_FRONTEND` | Implements PLAN.md comparative decision tree without fixed multipliers | Rendered in `ExposureRecoveryEngineCard` |
| `FailClosedSafeguards` (Data freshness, WebSocket, reconciliation) | `DERIVED_FRONTEND` | Monitored latencies and fail-closed gates | Rendered in `FailClosedSafeguardsCard` |
| `EmergencyKillSwitch` | `DERIVED_FRONTEND` / `PROPOSED_BACKEND` | Interactive manual and programmatic trigger; NATS topic planned | Fully interactive in UI-07 |

---

## 7. Research, Backtest & Stress Replay (UI-08)

| Field / Model | Status | Current Source / Notes | Temporary UI Behavior |
|---|---|---|---|
| `BacktestMetrics` (ROI, Ulcer Index, Drawdowns, Divergence) | `EXISTING` | Authoritative in `/api/quant/backtest/run` for historical crisis scenarios | Rendered in `BacktestReplayStudio` |
| `ExecutionFrictionAnalysis` (Naive vs realistic tick replay) | `DERIVED_FRONTEND` | Quantifies spread, fee, funding, latency, and partial fill slippage gap | Rendered in `ExecutionSensitivityCard` |
| `OverfittingMetrics` (DSR, PBO, Plateau stability, Complexity Budget) | `DERIVED_FRONTEND` | Strictly tracks 5 states, 4 engines, 5 grid levels, features <= 15 | Rendered in `OverfittingControlCard` |
| `PurgedCVFold[]` (Train / Purge / Test / Embargo) | `DERIVED_FRONTEND` | Models purged cross-validation to prevent serial correlation leakage | Rendered in `OverfittingControlCard` fold table |
| `DistributedWalkForwardRunner` | `PROPOSED_BACKEND` | Scheduled Python/DuckDB backtest worker cluster | Documented for future backend sprint |

---

## 8. Analytics & Performance Attribution (UI-09)

| Field / Model | Status | Current Source / Notes | Temporary UI Behavior |
|---|---|---|---|
| `BigQueryTelemetry` (Dry-run, scan cost, partitioned queries) | `EXISTING` | Authoritative via `/api/bigquery/query` & `BigQueryLakehouse` | Fully integrated in Lakehouse tab |
| `StrategyAttribution` (Gross PnL, Net PnL, Fees, Funding, Slippage) | `DERIVED_FRONTEND` | Decomposes virtual PnL attribution across Grid, Trend, Shock, Carry | Rendered in `StrategyAttributionCard` |
| `VirtualNettingAccounting` | `DERIVED_FRONTEND` | Computes net delta savings (+0.1 BTC target vs conflicting intents) | Illustrated in attribution banner |
| `ConservatismMetrics` (Zero-exposure time, rejected count, missed moves) | `DERIVED_FRONTEND` | Implements conservatism and excessive caution monitoring | Rendered in `ConservatismControlCard` |
| `FilterEfficacyAudit` (Return forfeited vs tail-risk avoided) | `DERIVED_FRONTEND` | Measures if filters save capital or destroy return | Rendered in filter audit table |
| `ScheduledAttributionPipeline` | `PROPOSED_BACKEND` | Nightly BigQuery scheduled query job for PnL reconciliation | Documented for future backend sprint |

---

## 9. Start Trading Wizard (UI-10)

| Field / Model | Status | Current Source / Notes | Temporary UI Behavior |
|---|---|---|---|
| `StartTradingFlow` | `DERIVED_FRONTEND` | Orchestrates Pre-flight Checks, Risk Setup, Strategy Allocation, and Confirmation. | Rendered in `StartTradingWizard` modal |
| `SystemCapabilityDiscovery` | `DERIVED_FRONTEND` | Verifies Binance Global API, Cloud Persistence, and NATS JetStream. | Displayed in Step 1 of Wizard |
| `RiskGovernorConstraints` | `EXISTING` | Ties into `account.risk_state` max drawdown and leverage. | Input sliders in Step 2 of Wizard |
| `StrategyEnablement` | `DERIVED_FRONTEND` | Enables/Disables Alpha Engines (Grid, Trend, Shock, Carry). | Checkboxes in Step 3 of Wizard |
| `SafetyAcknowledgement` | `DERIVED_FRONTEND` | Forces explicit authorization before transitioning to LIVE mode. | Explicit checkbox in Step 4 of Wizard |

---

## 10. Audit Log & Compliance Telemetry (UI-11)

| Field / Model | Status | Current Source / Notes | Temporary UI Behavior |
|---|---|---|---|
| `FirestoreAuditLogs` | `EXISTING` | Hooked up to `recordAuditLog` via `cloudAudit` wrapper. | Fetched dynamically in `AuditLogPage`. |
| `UserAuthenticationState` | `EXISTING` | Tied to `useAuth` Firebase context. | Displays auth warning block if not signed in. |
| `SystemOperationTelemetry` | `EXISTING` | `handleFirestoreError`, `BIGQUERY_QUERY_EXECUTE`, `GOOGLE_SHEETS_EXPORT`, etc. | Listed sequentially in feed. |
| `SystemAlertsCenter` | `EXISTING` | Dedicated system notifications (Shock, Funding, Risk breaches) with severity filtering and dismissal tracking via `AlertsDrawer` | Fully functional in UI-11 Alerts Center Drawer |

---

## 11. Production UX & Safety Hardening (UI-14)

| Field / Model | Status | Current Source / Notes | Temporary UI Behavior |
|---|---|---|---|
| `DestructiveActionInterceptor` | `DERIVED_FRONTEND` | Replaces `window.confirm` and inline prompt states with a unified modal. | Rendered via `ConfirmationModal` across app. |
| `TypeToConfirmGuard` | `DERIVED_FRONTEND` | Forces explicit intent (e.g. typing "KILL" or "CLOSE") for irreversible market actions. | Required in `ConfirmationModal` when `requireTypedConfirmation` is set. |
| `FailClosedNetworkSafety` | `EXISTING` | Disables destructive actions when API calls are in-flight. | Bound to `isActionLoading` loading states across buttons. |




---
## 12. System Truth & Safety Boundary (UI-15)
| Field / Model | Status | Current Source / Notes | Temporary UI Behavior |
|---|---|---|---|
| `TradingSystemState` (Data Source, Execution Mode, Engine State) | `EXISTING` | Authoritative backend state via `/api/system/state` | Replaces local frontend state with strict backend authority |
| `Preflight Checks` (Live vs Paper Guard) | `EXISTING` | Authoritative endpoint `/api/system/preflight` | Replaces simulated timeouts in StartTradingWizard |
| `ExecutionSource` (Order/Intent Provenance) | `EXISTING` | Extended on `ExecutionOrder` and intents | Displays SIMULATED, BINANCE_TESTNET, or BINANCE_LIVE badges to prevent UI spoofing |
| `Kill Switch Sync` | `EXISTING` | Synchronizes with `/api/system/state` | Enforces EMERGENCY state globally across all UI components |
