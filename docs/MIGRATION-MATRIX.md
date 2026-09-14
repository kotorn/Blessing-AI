# Core Module Migration Matrix

The repository recently migrated core strategy/risk components from older architecture to the authoritative `apps/trading_worker/engines` Python pipeline.

| Old Module | New Module | Behavior Preserved? | Tests Needed |
| :--- | :--- | :--- | :--- |
| `core/basket/models.py` | `domain/models.py` | Yes (Decimal precision maintained) | No |
| `core/basket/state_machine.py` | Python worker loop / `meta_allocator.py` | Partial (Implicitly handled, explicit state enforcements need unit tests) | Yes (State enforcements) |
| `core/events/schema.py` | `domain/models.py` | Yes | No |
| `core/grid/adaptive_grid.py` | `grid_strategy.py` | Partial (deterministic guardrails preserved; research scorer isolated) | Yes (Grid max depth, trend/shock brake, no aggressive Martingale) |
| `core/risk/governor.py` | `risk_governor.py` | Yes (Hard limits enforced before execution) | Yes (Risk Governor veto tests) |

## Audit Action Items

- **Basket State Machine Validation**: Ensure `meta_allocator.py` and `risk_governor.py` correctly validate state transitions (e.g., stopping additions when `NO_NEW_GRID`).
- **Unit Tests**: Python unit tests for `grid_strategy.py` (max depth), `meta_allocator.py` (conflict resolution), `risk_governor.py` (vetos), and `exposure_recovery.py`.
