# Research Replay Evidence Artifacts

This directory contains verifiable, content-addressed research replay artifacts generated according to **Plan §3.4 & §16** and validated by `apps/trading_worker/backtest/evidence_artifact.py`.

## Artifact Details: `evidence_artifact_btcusdt.json`

- **Artifact SHA-256**: `24e482100a197027a017d469b4e6cab44ab2ec2689fd0b385efa3854350dfb88`
- **Dataset ID**: `public-testnet-btcusdt-1m-sample`
- **Symbol**: `BTCUSDT`
- **Venue**: `BINANCE_TESTNET`
- **Market Type**: `USDM_FUTURES`
- **Code SHA**: Bound to commit SHA `53f23c5896507f401fd791e164e6b91b103cd4ee`
- **Cost Model**:
  - Maker fee: 0.0002 (2 bps)
  - Taker fee: 0.0005 (5 bps)
  - Market slippage: 2 bps
  - Funding interval: 60 sec
- **Verification Status**: `VERIFIED_REPLAY`

## Honesty & Safety Boundary

> [!IMPORTANT]
> **SIMULATED RESEARCH ONLY**:
> In accordance with Plan §3.4, simulated research replay evidence is **never** presented as live trading proof or a guarantee of profitability. Research modules have zero execution authority on live exchanges.

## Independent Verification

To independently verify the artifact against its embedded canonical hash and event dataset:

```python
from pathlib import Path
from apps.trading_worker.backtest.evidence_artifact import verify_replay_evidence_artifact

artifact_path = Path("docs/research/evidence_artifact_btcusdt.json")
verified = verify_replay_evidence_artifact(artifact_path)
print(f"Verified artifact digest: {verified.artifact_sha256}")
```
