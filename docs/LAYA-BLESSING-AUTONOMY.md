# Laya-style autonomy for Blessing AI

## Goal

Add an always-on `Observe -> Decide -> Route -> Execute -> Verify -> Learn`
control loop without changing Blessing AI's execution-authority invariant.

The Python Trading Worker remains the only mutable order authority.  Laya/JEV/
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
failed preflight into READY.  An AI advisory result may only downgrade the
permission chosen by deterministic policy; attempted upgrades are ignored and
audited.

## Supervisor actions

- `EMERGENCY`: emergency path / kill-switch semantics.
- `RECOVERY_ONLY`: no new risk; allow the existing worker recovery/close path.
- `PAUSE_NEW_RISK`: hold new or increased exposure while keeping observation and
  safer risk-reduction behavior alive.
- `OBSERVE_ONLY`: collect facts and recommendations, but do not run a risk-
  increasing strategy cycle.
- `ALLOW_PIPELINE`: permit the existing Blessing strategy -> allocator ->
  RiskGovernor pipeline to run.  This is not an order authorization.

## Laya concept mapping

| Laya concept | Blessing implementation |
| --- | --- |
| Event inbox | Worker/system/market/risk observations |
| Classify | deterministic policy + optional Laya/JEV shadow advisory |
| Action card | typed `AutonomyDecision` with reason codes |
| Approval | existing release approval / execution lease / ARM contract |
| Execute | existing Python Worker only |
| Verify | private stream + reconciliation + ledger |
| Learn | decision/firing logs and offline evaluation; never self-edit hard limits |

## Initial deployment levels

### A0 - Observe

Run the supervisor against PAPER/TESTNET state and persist decisions.  No
strategy routing changes.

### A1 - Shadow

Run Laya/JEV as `advisory_source=laya-shadow`.  Compare its action with the
hard policy.  AI advice cannot increase authority.

### A2 - Testnet gated automation

Use deterministic supervisor actions to gate strategy cycles on Testnet only.
Required before promotion: current-SHA mutating contract evidence, supervised
soak, restart/ambiguous-response recovery, and reconciliation evidence.

### A3 - Small Live staged-first-order

Keep the existing `STAGED_FIRST_ORDER` flow.  Start with the existing
conservative Mainnet launch contract and auto-pause after the first submission.
The supervisor may pause earlier but cannot skip release approval or Worker
preflight.

### A4 - Constrained continuation

Enable autonomous continuation only after the existing release controller and
operational evidence authorize it.  Keep per-order and daily risk bounds fixed
outside the AI layer.

## Wiring into the Worker

Create one `AutonomyObservation` immediately before a strategy evaluation tick
from the same authoritative Worker state used by readiness and risk gates.

Pseudo-flow:

```python
observation = AutonomyObservation(
    execution_mode=state.execution_mode,
    engine_state=state.engine_state,
    reconciliation_status=state.reconciliation_status,
    account_synchronized=state.account_synchronized,
    market_data_healthy=state.market_data_healthy,
    private_stream_healthy=state.private_stream_healthy,
    trading_connection_healthy=state.trading_connection_healthy,
    kill_switch_active=state.kill_switch_active,
    risk_state=risk_snapshot.risk_state,
    release_approved=state.release_approved,
    execution_lease_held=state.execution_lease_held,
    pending_ambiguous_execution=state.pending_ambiguous_execution,
)

decision = supervisor.step(observation, advisory_action=laya_shadow_action)

if decision.action == AutonomyAction.ALLOW_PIPELINE:
    run_existing_strategy_cycle()
elif decision.action == AutonomyAction.PAUSE_NEW_RISK:
    pause_new_risk_but_keep_reduction_paths()
elif decision.action == AutonomyAction.RECOVERY_ONLY:
    run_existing_recovery_path_only()
elif decision.action == AutonomyAction.EMERGENCY:
    run_existing_emergency_path()
```

The exact adapter from Worker state to `AutonomyObservation` should be added at
the narrowest existing pre-strategy orchestration point, rather than duplicating
readiness or risk calculations.

## Definition of done for "start trading"

The autonomy branch is ready to influence Testnet strategy cycles only when:

1. supervisor unit tests pass;
2. existing mainnet/testnet safety suites remain green;
3. current-SHA Testnet mutating contract is explicitly approved and recorded;
4. supervised soak produces zero unhandled execution exceptions;
5. crash/restart and ambiguous execution recovery are demonstrated;
6. no change weakens the existing release, lease, risk, reconciliation, or kill
   switch gates.

Mainnet remains a separate operational promotion.  The checked-in default stays
disarmed.
