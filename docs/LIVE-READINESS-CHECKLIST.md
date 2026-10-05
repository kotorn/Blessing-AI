> **SUPERSEDED.** This checklist predates the Local ETHUSDC pilot, the Track C
> signed-attestation readiness gate and the Docker Worker runtime. It is kept
> unchanged below for history only and must not be used to decide readiness or
> to start trading. Use instead:
>
> - `docs/LOCAL-PILOT-DAY-OF-CHECKLIST.md` (account, host and go/no-go checks)
> - `docs/LOCAL-PILOT-TRACK-C.md` (what the attestations prove and do not prove)
> - `docs/LOCAL-PILOT-BREAK-GLASS.md` (manual recovery)
> - `docs/MAINNET-RELEASE-RUNBOOK.md` (release and Local runtime gates)

# Live Execution Readiness Checklist

The `LIVE` mode is implemented behind dedicated credentials,
`MAINNET_LIVE_APPROVED=true`, and complete account/risk/stream/reconciliation
preflight. It remains hard-blocked by the checked-in default and this document
is not evidence that Live execution is operationally approved.

- [x] UI/UX Risk Acknowledgment
- [x] Truthful Execution Provenance (Source badges on orders)
- [x] Preflight Safety Verification endpoints
- [x] Global Kill Switch Synchronization
- [x] Single Execution Authority established (Python Trading Worker)
- [x] Fixed-environment native adapter implementation (`aiohttp`, `websockets`)
- [x] Testnet signature / timestamp synchronization and stream recovery
- [ ] CI Contract tests implemented and passing
- [ ] Supervised Testnet soak contract executed (opt-in via `allow_soak` and `confirm_soak` workflow_dispatch inputs)
- [ ] Unified release gate evaluated via `python -m apps.release_gate.gate` (combining offline `python -m apps.release_gate.repo_gate` and live-cloud `infra/release_gate/cloud_gate.ps1` requiring gcloud auth; fails closed on missing/stale cloud evidence)
- [ ] Mainnet execution separately approved, implemented, audited, and enabled

The `/api/system/preflight` and `/api/system/arm` endpoints keep `LIVE`
disarmed until the explicit approval flag and all Mainnet checks pass.
`liveExecutionReady` and `small_live_ready` remain false in the default
configuration.
