# Blessing AI — UI/UX Refactor Plan
## Trading Operating System for Multi-Strategy Execution
**Repository:** `kotorn/Blessing-AI`  
**Target release:** v0.2 UI Refactor  
**Primary venue:** Binance Global  
**Primary markets:** Spot + USDⓈ-M Futures  
**Frontend baseline:** React 19 + Vite + Tailwind CSS + TypeScript  
**Backend baseline:** Existing `server.ts` API plus Python quant/trading modules  
**Architecture principle:** Refactor the existing UI incrementally. Reuse working logic and components. Do not rewrite the trading engine.

---

# 1. Purpose

The current Blessing AI frontend already contains useful building blocks:
- Live Cockpit
- Balance & Wallets
- Stress Replay
- Quant Copilot
- BigQuery Lakehouse
- Architecture Review
- Binance API diagnostics
- Account overview
- Instrument monitoring
- Basket management
- Risk governor monitoring

The next step is not to add more independent widgets. The application should become a coherent **Trading Operating System** that answers, in order:
1. Is the system healthy?
2. What is the market doing?
3. Which strategies see opportunity?
4. What risk budget has each strategy received?
5. What target exposure does the portfolio want?
6. What orders are being sent and why?
7. What positions/baskets exist now?
8. Is recovery or deleveraging active?
9. How well are strategies performing?
10. Can every material decision be audited?

---

# 2. Refactor Principles

1. **Preserve working domain logic**: Do not rewrite basket state machine, grid calculation logic, risk governor logic, Binance adapter, auth logic, or BigQuery logic unless a UI contract requires a small adapter.
2. **Move from widgets to workflows**: Market → Signals / Price Action → Strategy Intent → Opportunity Score → Meta Allocation → Risk Approval → Target Exposure → Execution Decision → Exchange Order → Fill → Position / Basket → Recovery / Exit.
3. **Separate monitoring from control**: Monitoring may update frequently. Control actions must require explicit user intent, show consequences, be audited, support confirmation for dangerous actions, expose backend result/error state, and never silently fall back to local UI mutation in LIVE mode.
4. **Explainable by default**: Every material strategy action should trace why, what score, what budget, what target delta, what risk checks passed.
5. **Paper/Testnet/Live visual distinction**: Modes (RESEARCH, BACKTEST, PAPER, TESTNET, LIVE) must remain permanently visible in the global shell.

---

# 3. Global Shell & Navigation

Left persistent desktop navigation:
- **Command Center** (`/command`)
- **Markets** (`/markets`)
- **Strategies** (`/strategies`)
- **Orders & Execution** (`/orders`)
- **Positions & Baskets** (`/positions`)
- **Risk & Recovery** (`/risk`)
- **Portfolio** (`/portfolio`)
- **RESEARCH**
  - Replay & Backtest (`/research/replay`)
  - Analytics (`/analytics`)
- **SYSTEM**
  - Connections (`/system/connections`)
  - Audit Log (`/system/audit`)
  - Settings (`/settings`)

**Quant Copilot**: Global right-side drawer, accessible across all pages without losing context.
**Global Status Bar**: Slim bar showing Mode, Binance state, engine health, data freshness, risk state, Pause New Risk, Kill Switch, Alerts, and User.
