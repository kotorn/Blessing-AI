# Live Execution Readiness Checklist

The `LIVE` mode is hard-blocked in this repository. This document is a
future-only record of controls that would require a separate approved Mainnet
project; it is not evidence that Live execution is available.

- [x] UI/UX Risk Acknowledgment
- [x] Truthful Execution Provenance (Source badges on orders)
- [x] Preflight Safety Verification endpoints
- [x] Global Kill Switch Synchronization
- [x] Single Execution Authority established (Python Trading Worker)
- [x] Testnet-only native adapter implementation (`aiohttp`, `websockets`)
- [x] Testnet signature / timestamp synchronization and stream recovery
- [ ] CI Contract tests implemented and passing
- [ ] Mainnet execution separately approved, implemented, audited, and enabled

The `/api/system/preflight` and `/api/system/arm` endpoints explicitly reject
`LIVE` mode with a permanent sprint-level block. `liveExecutionReady` and
`small_live_ready` remain false.
