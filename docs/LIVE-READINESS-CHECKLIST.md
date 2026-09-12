# Live Execution Readiness Checklist

The `LIVE` mode is hard-blocked until the following conditions are met. This checklist tracks our progress towards full production live execution capability.

- [x] UI/UX Risk Acknowledgment
- [x] Truthful Execution Provenance (Source badges on orders)
- [x] Preflight Safety Verification endpoints
- [x] Global Kill Switch Synchronization
- [x] Single Execution Authority established (Python Trading Worker)
- [ ] Binance Live Adapter Implementation (CCXT/HTTPX)
- [ ] Signature / Timestamp synchronization guarantees (Clock Skew Management)
- [ ] Webhook / Listen Key subscription recovery (User Data Stream contract)
- [ ] CI Contract tests implemented and passing
- [ ] Live execution `capabilities.liveExecutionReady` flag enabled on backend

Until the Binance Live Adapter is fully completed and audited, the `/api/system/preflight` and `/api/system/arm` endpoints will explicitly reject `LIVE` mode with:
`Live Binance execution adapter is not production ready. Use PAPER or TESTNET.`
