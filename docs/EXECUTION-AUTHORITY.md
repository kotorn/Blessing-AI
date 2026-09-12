# Execution Authority

## Decision

The **Python Trading Worker** is the single authoritative execution engine for Blessing AI.

The TypeScript/Express backend acts purely as a Control Plane API and UI gateway. It does **not** generate strategy intents, evaluate risk, or place orders directly to the exchange.

### Architecture

```text
Web UI / Control Plane
        |
        v
Control API (TypeScript/Express)
        |
        v
Trading Worker (Python)
        |
        +-- Market Data
        +-- Price Action
        +-- Market State
        +-- Strategies
        +-- Meta Allocator
        +-- Risk Governor
        +-- Exposure Recovery
        +-- Target Exposure
        +-- Execution Optimizer
        |
        v
Execution Adapter (venues/binance/execution.py)
        |
        v
Binance USDⓈ-M Futures
```

## Anti-Split-Brain Invariant

There must NOT be a separate TypeScript execution engine and Python execution engine both capable of submitting orders. This avoids split-brain concurrency risks.

## Ledger Consolidation

The authoritative fill and position ledger is maintained by the Python worker, which persists state or surfaces it via standardized data schemas to the Control API. The frontend only reads this ledger; it does not fabricate execution traces or order events independently.

## Provenance

Every order/decision logged by the Python worker must explicitly denote its provenance and environment mode (e.g., `[PAPER]`, `[TESTNET]`, `[RESEARCH]`).
