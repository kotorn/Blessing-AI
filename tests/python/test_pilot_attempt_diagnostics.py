"""Operators must be able to see why a pilot signal did not become an order."""

from types import SimpleNamespace

from apps.trading_worker.main import TradingWorkerApp


def _worker() -> TradingWorkerApp:
    return TradingWorkerApp(symbols=["ETHUSDC"])


def test_state_exposes_empty_diagnostics_before_any_signal():
    diag = _worker().get_state().pilot_attempt_diagnostics
    assert diag["signals"] == 0
    assert diag["last_stage"] is None
    assert diag["last_reason"] is None
    assert diag["last_signal_at"] is None


def test_each_stage_is_counted_and_last_reason_is_kept():
    worker = _worker()
    worker._note_pilot_attempt("SIGNAL")
    worker._note_pilot_attempt("PREPLAN_FAILED", "Pilot bracket derivation failed: no cost evidence")
    worker._note_pilot_attempt("SIGNAL")
    worker._note_pilot_attempt("EXECUTION_BLOCKED", "Market data stale for ETHUSDC")
    diag = worker.get_state().pilot_attempt_diagnostics
    assert diag["signals"] == 2
    assert diag["preplan_failed"] == 1
    assert diag["execution_blocked"] == 1
    assert diag["last_stage"] == "EXECUTION_BLOCKED"
    assert diag["last_reason"] == "Market data stale for ETHUSDC"
    assert diag["last_signal_at"]


def test_reason_is_truncated_and_stripped_of_control_characters():
    worker = _worker()
    worker._note_pilot_attempt("EXECUTION_ERROR", "bad\nreason\x00" + "x" * 1000)
    reason = worker.get_state().pilot_attempt_diagnostics["last_reason"]
    assert len(reason) <= 200
    assert "\n" not in reason and "\x00" not in reason


def test_adapter_order_gate_block_is_surfaced_when_present():
    worker = _worker()
    worker.execution_adapter = SimpleNamespace(
        last_order_block={"stage": "FINAL_FENCE", "reason": "Final order risk inputs changed"}
    )
    diag = worker._pilot_attempt_diagnostics()
    assert diag["adapter_last_block"] == {
        "stage": "FINAL_FENCE",
        "reason": "Final order risk inputs changed",
    }
