# Laya-style autonomy for Blessing AI

## Goal

Add an always-on `Observe -> Decide -> Route -> Execute -> Verify -> Learn`
control loop without changing Blessing AI's execution-authority invariant.

The Python Trading Worker remains the only mutable order authority. Laya/JEV/
LLM output is advisory and cannot bypass `RiskGovernor`, the worker decision
and order gates, execution lease, reconciliation, release approval, or the kill
switch.

## Control loop

```text
Binance + Worker + market/risk state
              |
              v
         OBSERVE SNAPSHOT
              |
              v
  DeterministicAutonomyPolicy
              |
       +------+------+
       |             |
       v             v
 Laya/JEV shadow   hard safety facts
 advisory only        |
       |              |
       +------v-------+
       AutonomySupervisor
              |
   permission only; no OrderIntent
              |
   +----------+----------+----------------+
   |          |          |                |
OBSERVE    PAUSE      RECOVERY         ALLOW
 ONLY      NEW RISK     ONLY          PIPELINE
                                      |
                                      v
                           existing strategies
                                      |
                                      v
                               MetaAllocator
                                      |
                                      v
                                RiskGovernor
                                      |
                                      v
                         decision gate -> order gate
                                      |
                                      v
                           Binance execution adapter
                                      |
                                      v
                     user stream + reconciliation
                                      |
                                      +----> next observation
```

## Non-negotiable authority boundary

The supervisor never creates `OrderIntent`, changes leverage, changes hard risk
limits, grants a release approval, acquires an execution lease, or turns a
failed preflight into READY.

The AI advisory vocabulary is intentionally smaller than the deterministic
Worker action vocabulary:

- `CONTINUE`
- `PAUSE_NEW_RISK`
- `OBSERVE_ONLY`

Laya/JEV/LLM cannot request `RECOVERY_ONLY` or `EMERGENCY`; those paths can
perform mutable risk-reduction actions and therefore remain driven only by
authoritative Worker facts.

## Supervisor actions

- `EMERGENCY`: preserve the existing emergency/kill-switch path.
- `RECOVERY_ONLY`: no new risk; preserve the existing recovery/close path.
- `PAUSE_NEW_RISK`: hold new or increased exposure while safer reductions stay available.
- `OBSERVE_ONLY`: collect facts/recommendations without enabling strategy risk.
- `ALLOW_PIPELINE`: permit the existing strategy -> allocator -> RiskGovernor pipeline.
  This is still not an order authorization.

## Laya concept mapping

| Laya concept | Blessing implementation |
| --- | --- |
| Event inbox | Worker/system/market/risk observations |
| Classify | deterministic policy + optional Laya/JEV shadow advisory |
| Action card | typed `AutonomyDecision` with reason codes |
| Approval | existing release approval / execution lease / ARM contract |
| Execute | existing Python Worker only |
| Verify | private stream + reconciliation + durable ledger |
| Learn | decision/firing logs and offline evaluation; never self-edit hard limits |

## Deployment levels

### A0 - Observe

Run the supervisor against PAPER and connected state and persist decisions. No
strategy routing changes.

### A1 - Shadow

Run Laya/JEV as `advisory_source=laya-shadow`. Compare its recommendation with
the deterministic policy. AI advice can only reduce activity.

### A2 - Testnet automation (optional evidence path)

Use deterministic supervisor actions to gate strategy cycles on Testnet. This
remains useful for regression testing, but it is not a mandatory prerequisite
for the currently accepted first ETHUSDC Mainnet launch policy.

`docs/DECISION-2026-09-21-skip-testnet-evidence.md` explicitly waives the
Testnet readonly/mutation/soak evidence chain for the first Mainnet launch. It
does **not** waive any Mainnet release gate.

### A3 - Small Live staged first order

Follow `docs/MAINNET-RELEASE-RUNBOOK.md` Gate 1..5 exactly. The current fixed
launch contract is ETHUSDC, `STAGED_FIRST_ORDER`, at most 250 USDC collateral,
at most 1,000 USDC gross exposure, first order at most 50 USDC, daily loss at
most 5 USDC, leverage at most 10x, and exactly one active exposure chain.

The supervisor may pause earlier, but it cannot create approval, acquire a
lease, ARM the Worker, or bypass preflight. After the first submission the
existing Worker pauses new risk.

### A4 - Constrained autonomous continuation

Follow Gate 6. Only the existing second one-time `trading_admin` approval and
Worker continuation transition may enter `AUTONOMOUS_ACTIVE`. Laya becomes an
always-on supervisory/advisory loop inside those fixed safety limits; it never
self-expands the limits.

A restart/revision change remains `REAUTH_REQUIRED`/DISARMED as specified by the
existing runbook. Conversation state is never an authorization to resume.

## Wiring into the Worker

Create one `AutonomyObservation` immediately before strategy-intent allocation,
using the same authoritative Worker and adapter facts already used by readiness
and execution gates.

Current-code mapping should use canonical fields rather than inventing duplicate
readiness state:

```python
adapter = self.execution_adapter
lease = getattr(adapter, "execution_lease", None) if adapter else None

observation = AutonomyObservation(
    execution_mode=self.execution_mode.value,
    engine_state=self.engine_state.value,
    reconciliation_status=self.reconciliation_status,
    account_synchronized=self.account_synchronized,
    market_data_healthy=self.market_data_healthy,
    private_stream_healthy=self.private_stream_healthy,
    trading_connection_healthy=self.trading_connection_healthy,
    kill_switch_active=self.kill_switch_active,
    risk_state=risk_snapshot.risk_state,
    release_approved=(
        self.execution_mode.value != "LIVE" or self.mainnet_live_approved
    ),
    execution_lease_held=(
        self.execution_mode.value != "LIVE"
        or (
            lease is not None
            and getattr(lease, "fencing_token", None) is not None
        )
    ),
    pending_ambiguous_execution=False,  # bind to the canonical ambiguity flag/state
)

decision = supervisor.step(observation, advisory_action=laya_shadow_action)
```

The exact ambiguity mapping must come from the existing canonical execution /
reconciliation state; do not add a second independently mutable flag merely to
feed the supervisor.

The integration point is immediately before the existing `MetaAllocator`
allocation. Existing strategies may still calculate intents while shadowing;
`AutonomyDecision` determines whether those intents may advance toward the
existing allocator/risk pipeline.

## Definition of done for the current first Mainnet launch

The Laya integration may participate in first-live supervision when:

1. supervisor tests and the existing CI suite are green;
2. the supervisor is wired without weakening any existing Worker gate;
3. Mainnet runbook Gate 1 repository/identity/schema evidence passes;
4. Gate 2 Control Plane authentication/readiness passes;
5. Gate 3 LIVE-disarmed Mainnet read-only preflight passes with zero order attempts;
6. Gate 4 candidate verification and independent approval passes;
7. Gate 5 performs only the existing capped staged-first-order flow;
8. continuation remains blocked until Gate 6 evidence plus a second explicit approval.

The Testnet evidence waiver changes the evidence route, not the risk authority.
The checked-in default remains disarmed.
