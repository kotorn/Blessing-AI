# Local Live Research Pilot: Success & Abort Criteria

## 1. Scope & Objective
This document defines the formal success, completion, and abort criteria for the **Local Live Research Pilot (Plumbing Test)** on Binance USDⓈ-M / Portfolio Margin Futures for `ETHUSDC`.

The primary goal of this pilot is **end-to-end operational verification** of the trading pipeline, risk enforcement, signed Track C evidence binding, and durable protection brackets with real money at minimal risk:
- **Symbol:** `ETHUSDC`
- **Instruments:** Exactly 1 instrument (`ETHUSDC`)
- **Management Mode:** `QUICK`
- **Max Notional Cap:** ≤ 50.0 USDC per order
- **Max Stop Risk Cap:** ≤ 2.0 USDC
- **Maximum Aggregate Loss Cap:** ≤ 5.0 USDC
- **Campaign Execution Limit:** Exactly 1 staged entry order

---

## 2. Success Criteria

The pilot is deemed **SUCCESSFUL** if and only if all of the following stages complete in sequence with zero safety violations:

### A. Stage 1: Preparation & Preflight Gate
1. Host and Containerized Worker run with identical git commit SHA matching the signed Track C release binding.
2. Control Plane verifies all 5 Track C evidence classes (`CHECKS`, `REVIEW_AUTH_RELEASE`, `REVIEW_ORDER_RISK`, `REVIEW_PERSISTENCE`, `TESTNET_ETHUSDC`) within their 24-hour validity window.
3. Control Plane issues an HMAC-signed short-lived `PilotReadinessVerdict` to the containerized Worker.
4. Worker verifies the HMAC signature, campaign ID, git SHA, and 5-field hashes against its container environment.
5. All 19 preflight safety checks pass (`PASS`) and system transitions to `LIVE/DISARMED`.

### B. Stage 2: Operator Arming & Staged Entry
1. Operator explicitly ARMs the stack for `STAGED_FIRST_ORDER`.
2. Exactly one order intent is evaluated by the Worker risk gate and clamped by `plan_pilot_bracket`:
   - Side: `BUY` or `SELL`
   - Position Mode: One-Way (`BOTH`)
   - Notional: ≤ 50.0 USDC
   - Planned Stop Risk: ≤ 2.0 USDC
   - Minimum Net Reward: ≥ 0.20 USDC
3. Entry order fills on Binance exchange (`FILLED`).

### C. Stage 3: Exchange-Native Protection Bracket Verification
1. Both conditional protection orders are submitted and verified directly on the exchange within the protection timeout:
   - **Stop Loss:** `STOP_MARKET`, mark price trigger, reduce-only, quantity matching entry fill.
   - **Take Profit:** `TAKE_PROFIT_MARKET`, mark price trigger, reduce-only, quantity matching entry fill.
2. The protection state is durably persisted and verified on Binance exchange (`PROTECTED_VERIFIED`).
3. System state transitions to `pause_new_risk = True` so no additional entries can be submitted.

### D. Stage 4: Position Closure
The position is closed through one of the following legitimate exit paths:
1. **Take-Profit Trigger:** Market reaches the TP price; Binance executes the TP order and cancels/expires the SL order.
2. **Stop-Loss Trigger:** Market reaches the SL price; Binance executes the SL order and cancels/expires the TP order.
3. **24-Hour Expiry Close:** If neither trigger fires within 24 hours, the position is automatically or manually closed via reduce-only market order.
4. **Operator Break-Glass Close:** Operator issues emergency flatten or manual close via Binance UI.

### E. Stage 5: Final Reconciliation & Clean State Verification
Immediately following position closure:
1. **Position Exposure:** Net position in `ETHUSDC` equals `0.000` (flat verified on exchange position risk endpoint).
2. **Open Orders:** Zero (0) open regular orders on `ETHUSDC`.
3. **Open Algo Orders:** Zero (0) open conditional algo orders on `ETHUSDC` (no dangling SL/TP orders).
4. **Reconciliation State:** Worker reconciliation reports `IN_SYNC` with `diff_count == 0`.
5. **Durable Ledger:** All fills, trades, and fees are recorded with zero unhandled discrepancies.

---

## 3. Abort & Fail-Closed Criteria

The pilot must be **IMMEDIATELY ABORTED** (triggering Kill Switch and operator intervention) if any of the following occur:

| Condition | Trigger / Detection | Required Action |
| :--- | :--- | :--- |
| **Missing Protection Bracket** | Either SL or TP algo order fails to place or confirm within 15 seconds of entry fill. | Trigger emergency flatten immediately. If Worker fails to flatten, operator closes via Binance App/Web immediately. |
| **Attestation Clock Expiration** | 24-hour attestation window expires before order execution. | Worker automatically disarms and refuses `can_start`. Refresh Track C evidence before proceeding. |
| **Verdict Signature Failure** | Worker receives invalid or expired HMAC verdict from Control Plane. | Worker disarms instantly and enters fail-closed state (`can_start = False`). |
| **Reconciliation Desync** | Worker detects position or order state mismatch (`DEGRADED` or diff > 0). | Activate kill switch, halt trading, operator inspects exchange state manually. |
| **WebSocket / Heartbeat Loss** | Private user data stream or Control Plane heartbeat drops for > 30 seconds. | Worker pauses risk; if prolonged, Worker disarms. Operator verifies position status on Binance. |
| **Account Exposure Leak** | Any position or order detected outside `ETHUSDC` or exceeding the 50 USDC cap. | Emergency flatten all positions, revoke API keys, freeze system. |

---

## 4. Operator Attended Protocol
1. **Dual-Screen Attendance:** During the armed window and until position closure, the operator must have the Blessing AI Command Center and the Binance Futures web/mobile app open simultaneously.
2. **Break-Glass Readiness:** Operator must have immediate access to:
   - Red "Kill Switch" button in Blessing AI UI.
   - Binance Futures "Close All Positions" and "Cancel All Open Orders" buttons.
3. **Zero Automated Re-arm:** Under no circumstances should the system automatically re-arm after an abort or successful completion.
