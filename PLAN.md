# Blessing AI v0.2 — Google SaaS-First Architecture Plan

> **Note**: This file mirrors `Blessing-AI-v0.2-Plan.md` as the primary architectural plan for the project.

> **Current implementation boundary (2026-09-22):** The repository uses
> Firestore as the UI authority and Cloud SQL as the Worker operational
> authority for v0.2. `VITE_DATA_CONNECT_CUTOVER=false` is enforced; Data
> Connect cutover is deferred to v0.3 by
> [`docs/ADR-2026-09-22-dataconnect-deferral-v0.3.md`](docs/ADR-2026-09-22-dataconnect-deferral-v0.3.md).
> PAPER/default and fail-closed execution remain mandatory. This architecture
> document does not authorize deployment, Mainnet arming, order submission,
> secret rotation, billing changes, or database migration.

Please refer to [`Blessing-AI-v0.2-Plan.md`](./Blessing-AI-v0.2-Plan.md) for the complete, authoritative specification.

## Core Summary

1. **Target**: Binance Global (Spot & USDⓈ-M Futures) for BTCUSDT, ETHUSDT (SOLUSDT & BNBUSDT later).
2. **Infrastructure**: Google SaaS-First MVP (Cloud SQL PostgreSQL, Firebase Hosting & Auth, Firebase Data Connect, Cloud Run Worker, BigQuery, Google Cloud Storage, Secret Manager, Cloud Logging/Monitoring).
3. **Trading Loop**: `Market Data -> Price Action / Microstructure / Shock -> Market State -> Strategies (Grid, Trend, Shock, Carry) -> Opportunity Engine -> Meta Allocator -> Portfolio Risk Governor -> Exposure Recovery -> Target Exposure -> Execution Optimizer -> Binance Adapter`.
4. **Tri-Tier Persistence**:
   - **HOT**: Cloud SQL PostgreSQL (zero high-frequency ticks, only operational & basket state).
   - **WARM**: Google BigQuery (partitioned analytical research lakehouse).
   - **COLD**: Google Cloud Storage (`gs://blessing-ai-data`) in Apache Parquet format.
5. **Decoupled Architecture**: Firebase and BigQuery are outside the critical order execution loop. The Cloud Run trading worker operates as an autonomous daemon.
6. **Default Mode**: `PAPER`. Strict risk gates before live pilot.
