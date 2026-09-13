# Blessing AI v0.2 — Google Cloud Infrastructure Cost Model

This document outlines the projected operational cost model for **Blessing AI v0.2** across its key lifecycle stages. The goal during Months 1–6 is to test whether the trading strategies produce a sustainable edge with minimal infrastructure overhead.

---

## 1. Services in Scope

The SaaS-first infrastructure model utilizes the following Google Cloud services:
1. **Google Cloud SQL for PostgreSQL**: Relational transactional database for hot operational state (baskets, orders, positions, risk states).
2. **Google Cloud Run Worker**: Containerized trading daemon running 24/7 with always-on CPU (`--no-cpu-throttling`, `min-instances=1`).
3. **Google BigQuery**: Partitioned analytical lakehouse for feature storage, trade analytics, and backtest results.
4. **Google Cloud Storage (GCS)**: Columnar Apache Parquet storage for buffered raw Binance market data.
5. **Google Secret Manager**: Secure storage for Binance API keys and database credentials.
6. **Google Cloud Logging & Cloud Monitoring**: Centralized structured logging, health metrics, and alert triggers.
7. **Network Egress**: WebSocket bi-directional traffic to Binance Global endpoints.
8. **Firebase Services (Auth, Hosting, Data Connect)**: Client application hosting, authentication, and GraphQL data connector.

---

## 2. Infrastructure Cost Projection by Stage

### Summary Table (Monthly Cost in USD)

| Component | Stage A: Dev / Research | Stage B: Paper Trading 24/7 | Stage C: Small Live Pilot | Stage D: Multi-Asset Scale |
| :--- | :--- | :--- | :--- | :--- |
| **Focus** | Local dev, backtesting, prototyping | 24/7 autonomous paper run (BTC, ETH) | Real capital ($1k), strict risk (BTC, ETH) | Multi-pair (BTC, ETH, SOL, BNB) |
| **Cloud SQL (Postgres 17)** | $0.00 (Local / Free) | $9.50 (`db-f1-micro` + 10GB SSD) | $45.00 (`db-custom-1-3840`, 25GB SSD) | $55.00 (`db-custom-1-3840`, 50GB SSD) |
| **Cloud Run Worker** | $0.00 (On-demand testing) | $28.00 (1 vCPU, 2GB, always-on) | $28.00 (1 vCPU, 2GB, always-on) | $56.00 (2 vCPU, 4GB or 2 workers) |
| **BigQuery (Storage & Query)**| $0.00 (Within 1TB free tier) | $1.50 (Storage ~20GB, query scans) | $3.00 (Storage ~50GB, query scans) | $8.00 (Storage ~150GB, query scans) |
| **Cloud Storage (GCS)** | $0.10 (<5 GB Parquet) | $1.00 (~50 GB Parquet) | $2.50 (~120 GB Parquet) | $6.00 (~300 GB Parquet) |
| **Google Secret Manager** | $0.00 (Free tier <6 secrets) | $0.06 (Versions & access calls) | $0.06 (Versions & access calls) | $0.10 (Versions & access calls) |
| **Cloud Logging & Monitoring**| $0.00 (Within 50 GiB free tier)| $2.00 (~5 GiB billable over free) | $4.00 (~10 GiB billable) | $8.00 (~20 GiB billable) |
| **Firebase (Hosting & Auth)** | $0.00 (Spark free tier) | $0.00 (Spark free tier) | $0.00 (Spark free tier) | $5.00 (Blaze pay-as-you-go) |
| **Network Egress (Binance WS)**| $0.50 (Intermittent testing) | $2.50 (~25 GB egress) | $3.50 (~35 GB egress) | $7.00 (~70 GB egress) |
| **ESTIMATED TOTAL / MONTH** | **~$1.00 / month** | **~$44.56 / month** | **~$86.06 / month** | **~$145.10 / month** |

---

## 3. Detailed Stage Breakdown

### Stage A — Development & Research (Month 1)
- **Activity**: Initial codebase setup, Binance capability discovery, strategy unit testing, local backtesting on historical samples.
- **Compute**: Local machine execution or sporadic Cloud Run test revisions.
- **Database**: Local PostgreSQL container or development Cloud SQL instance spun down when idle.
- **Data Ingest**: Sample data buffers written to GCS for schema validation.
- **Estimated Monthly Cost**: **< $5.00** (well within standard Google Cloud free trial credits).

### Stage B — Paper Trading 24/7 (Months 2–3)
- **Activity**: Continuous 24/7 paper trading on **BTCUSDT** and **ETHUSDT** (Spot and USDⓈ-M Futures). Testing reconnection stability, reconciliation after process restart, and fail-closed state machines.
- **Compute**: Single Cloud Run Worker instance (1 vCPU, 2 GiB RAM) configured with:
  ```bash
  gcloud run deploy blessing-trading-worker \
    --image gcr.io/blessing-ai/trading-worker:v0.2 \
    --no-cpu-throttling \
    --min-instances 1 \
    --max-instances 1 \
    --memory 2Gi \
    --cpu 1
  ```
- **Database**: Cloud SQL `db-f1-micro` (1 shared vCPU, 0.6 GB RAM, 10 GB SSD storage) located in `asia-southeast1` (Singapore) or `us-central1`.
- **GCS & BigQuery**: Ring-buffer flushes 50 MB Parquet batches every 5 minutes (~14.4 GB raw daily compressed to ~3 GB Parquet). Older raw ticks archived; only aggregated 1m OHLCV and engineered features retained long-term.
- **Estimated Monthly Cost**: **~$45.00 / month**.

### Stage C — Small Live Pilot (Months 4–5)
- **Activity**: Live trading with real capital on Binance Global ($1,000 maximum capital allocation). Strictly enforcing BTCUSDT and ETHUSDT only, maximum leverage $\le 2.0\times$.
- **Database Upgrade**: Cloud SQL upgraded to `db-custom-1-3840` (1 dedicated vCPU, 3.75 GB RAM) to guarantee sub-5ms commit latencies on order execution and fill logging.
- **Compute**: Same single Cloud Run Worker instance with strict memory limits and Prometheus/Cloud Monitoring health pings.
- **Estimated Monthly Cost**: **~$86.00 / month**.

### Stage D — Multi-Instrument Scale (Months 6+)
- **Activity**: Adding **SOLUSDT** and **BNBUSDT**. Expanding the Basis Carry strategy across all 4 instruments.
- **Compute**: Upgraded Cloud Run instance (2 vCPU, 4 GiB RAM) or evaluating migration to a dedicated Google Compute Engine `e2-standard-2` VM (~$49/mo) if persistent TCP sockets offer superior latency.
- **Database**: Cloud SQL with 50 GB SSD storage and automated storage auto-increase enabled.
- **BigQuery**: Daily partitioning and monthly clustering maintenance routines.
- **Estimated Monthly Cost**: **~$145.00 / month**.

---

## 4. Cost Governance & Runaway Prevention

### Runtime carry and research-evidence boundary

The funding-carry engine does not use a built-in “typical” fee, spread,
slippage, financing, or weekly-rebalance assumption. It emits no carry intent
until all `CARRY_*` inputs in `.env.example` are supplied. The resulting
horizon calculation records gross funding income and each explicit cost
component separately before applying the minimum net annualized-yield filter.

`apps/trading_worker/backtest/evidence.py` evaluates externally generated
trades using purged/embargoed chronological test windows, regime coverage, and
neighboring parameter-variant stability. Research candles must be timezone-aware,
strictly chronological, contiguous 1-minute observations with a complete
forward-label horizon. Trade-level funding, spread, and slippage observations
are mandatory, duplicate trade IDs are rejected, and unknown-regime OOS trades
prevent research-quality status. A quality result also requires every adjacent
parameter variant to identify the same OOS folds and a train-only selection
record bound to each fold's actual train window. These are structural audit
checks over externally generated research artifacts; they do not prove that an
artifact exists or authorize Worker execution. A positive result is still
`RESEARCH_ONLY`. The ML scorer remains disabled until a real versioned model and
out-of-sample evidence exist; no fabricated probability is used.

To ensure costs never exceed experimental budgets:

1. **Google Cloud Budget Alerts**:
   - Threshold 1: **$50.00** (50% of expected budget) -> Automated email alert.
   - Threshold 2: **$80.00** (80% of expected budget) -> High-priority notification.
   - Threshold 3: **$100.00** (100% of expected budget) -> Escalation to stop non-critical research queries.

2. **BigQuery Cost Controls**:
   - **Mandatory Partition Filtering**: All analytical queries must filter on `timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)` to prevent full-table scans.
   - **Clustering**: Key tables are clustered by `[symbol, regime]` or `[strategy_id, model_version]`, reducing scanned bytes by 80–95%.
   - **Dry Run Verification**: All automated research scripts invoke the BigQuery `dry_run=True` flag to compute scanned bytes before query dispatch. Queries exceeding 10 GB scan are rejected by default.

3. **GCS Lifecycle Policies**:
   - Raw L2 orderbook diffs transition from `Standard` to `Nearline` storage after 30 days, reducing storage costs from $0.020/GB to $0.010/GB.
   - Non-critical raw trade archives older than 90 days are deleted or moved to `Coldline` storage.

4. **Zero-Tick Rule for PostgreSQL**:
   - Under no circumstances may high-frequency WebSocket ticks be inserted row-by-row into Cloud SQL. Only order updates, fill executions, strategy state transitions, and 1-second portfolio snapshots are persisted to PostgreSQL.
