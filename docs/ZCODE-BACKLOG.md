# Blessing AI v0.2 — ZCODE readiness backlog

Status is evidence-based. `OPEN` and `NOT_RUN` are intentional states; they
must not be converted to `PASS` from configuration or CI alone.

| ID | Priority | Workstream | Acceptance evidence | Status |
|---|---|---|---|---|
| ZC-001 | P0 | Artifact Registry retention | Read-only image audit, protected digest verification, conservative policy reviewed; no deletion applied by this PR | IMPLEMENTED / OPERATOR APPLY OPEN |
| ZC-002 | P0 | Live cost baseline | Timestamped Cloud Run, SQL, registry, budget, monitoring and secret-name read-back; billing totals remain unavailable unless exported | IMPLEMENTED |
| ZC-003 | P1 | Evidence provenance UI | Fresh server timestamp, worker heartbeat, and explicit VERIFIED/SIMULATED/STALE/UNAVAILABLE labels | IMPLEMENTED / UAT OPEN |
| ZC-004 | P1 | Start Trading Wizard | Authentication, role, worker heartbeat, persistence and server preflight checks visibly block unsafe ARM | IMPLEMENTED / AUTH UAT OPEN |
| ZC-005 | P1 | Runtime profiles | Control Plane deploy/verify scripts support bounded DEV_PAPER_UI and MAINNET_OPERATOR_UI profiles | IMPLEMENTED / REMOTE ROLLOUT OPEN |
| ZC-006 | P1 | Staging UAT | Named URL baseline, browser evidence, and explicit NOT_RUN reasons for unavailable roles/fault injection | OPEN UNTIL UAT EVIDENCE |
| ZC-007 | P1 | Data Connect decision | ADR records Firestore authority and v0.3 deferral with cutover=false | IMPLEMENTED |
| ZC-008 | P1 | Release report | Full local gates, current-SHA CI, UAT evidence, and remaining operator gates are recorded truthfully | OPEN UNTIL PR CHECKS |
| ZC-009 | P2 | Testnet evidence | Existing waiver remains in force; no new Testnet contract/soak claim is made by this PR | WAIVED BY DECISION |

## Non-goals for this PR

- No Mainnet arming, order submission, kill-switch mutation, secret rotation,
  billing change, database migration, or Artifact Registry cleanup.
- No Data Connect cutover or Firestore-to-SQL backfill.
- No claim that the current Cloud Run URL has received this branch until a
  separately approved deployment and destination read-back exist.
