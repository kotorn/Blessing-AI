# Small-Live Mainnet Execution Checklist

## Executive Summary
This document serves as the absolute checklist before authorizing `LIVE` mode (Mainnet) for Blessing AI v0.2. The system must operate flawlessly in autonomous `TESTNET` before any of these gates are passed.

## Pre-Requisite
- [ ] 72-Hour Autonomous Testnet Soak completed with ZERO unhandled exceptions.
- [ ] Portfolio risk governance effectively managed a simulated volatility shock (e.g., using a manual script to mutate positions).

## 1. Network & Infrastructure
- [ ] Environment variables configured correctly (no TESTNET keys in LIVE config).
- [ ] Exchange domain endpoints correctly toggle to Mainnet per `BinanceEnvironment`.
- [ ] REST API and WebSocket rate limiters tuned to Mainnet thresholds (which are typically stricter).
- [ ] NATS JetStream / PostgreSQL persistence layer active for disaster recovery (not just in-memory ledger).

## 2. Order Execution & Ledger
- [ ] Ledger durability verified. If the process crashes mid-order, NATS/Postgres captures the intent and reconciliation syncs it.
- [ ] Idempotency confirmed via `newClientOrderId` using deterministic hashing.
- [ ] Reconciliation tested against actual LIVE latency.
- [ ] Account snapshot updates (Margin, Equity) rely purely on User Stream `ACCOUNT_UPDATE` and background `/fapi/v2/account` sync.

## 3. Risk & Safety Limits
- [ ] **Hard Coded Ceiling:** Max single order notional capped at a very conservative value for Small-Live (e.g., $100).
- [ ] **Max Open Orders:** Limit total open orders globally to < 5.
- [ ] **Drawdown Governor:** Hard kill switch if drawdown exceeds Small-Live budget (e.g., 2%).
- [ ] **Kill Switch:** Validated that activating the kill switch securely cancels all LIVE open orders and blocks new risk.

## 4. Strategy & Meta Allocator
- [ ] `MarketScannerEngine` strictly limited to high liquidity pairs (e.g., `BTCUSDT`, `ETHUSDT`) for the first 30 days.
- [ ] Grid spacing widened for Mainnet execution.
- [ ] Trend / Shock strategies disabled or set to simulated-only for the first week to isolate Grid evaluation.

## 5. Operations
- [ ] Logging aggregated and searchable.
- [ ] Real-time alerts (e.g., Telegram, Slack) configured for Kill Switch activation, Reconciliation failure, and Drawdown breaches.
- [ ] Runbook established for manual intervention.

## Authorization
- [ ] Engineering Lead Sign-off
- [ ] Risk Lead Sign-off
