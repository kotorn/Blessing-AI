"""Run the no-skip Local Pilot lease and protection CI acceptance cases."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


GROUPS = (
    (
        "LEASE_FENCING",
        (
            "tests/python/test_execution_lease.py::test_execution_lease_serializes_same_account_scope_and_fences_old_owner",
            "tests/python/test_execution_lease.py::test_lease_loss_is_checked_immediately_before_order_post",
            "tests/python/test_local_mainnet_gate.py::test_final_order_fence_rejects_stalled_pilot_lifecycle_monitor",
            "tests/python/test_local_mainnet_gate.py::test_final_order_fence_rechecks_monitor_after_async_risk_gate",
        ),
        4,
    ),
    (
        "PROTECTION_CLOSE",
        (
            "tests/python/test_binance_protection.py::test_terminal_partial_fill_is_protected_when_algos_cover_actual_position",
            "tests/python/test_binance_protection.py::test_local_mainnet_ambiguous_close_is_degraded_and_never_resubmitted",
            "tests/python/test_local_pilot_review_recovery.py::test_quick_timeout_closes_despite_unavailable_accounting",
            "tests/python/test_local_pilot_review_recovery.py::test_concurrent_close_reload_and_atomic_claim_allow_one_submission",
            "tests/python/test_history_review_fencing.py::test_concurrent_claims_and_attempts_have_single_winner",
        ),
        6,
    ),
)
SUMMARY = re.compile(r"(?P<count>\d+) passed in \d+(?:\.\d+)?s")


def _run_group(root: Path, name: str, nodes: tuple[str, ...], expected: int) -> str:
    command = [
        sys.executable, "-m", "pytest", "-q", "--color=no", "--tb=short",
        "-p", "no:cacheprovider", *nodes,
    ]
    env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "APPDATA", "USERPROFILE")
        if key in os.environ
    }
    env.update({
        "MAINNET_LIVE_APPROVED": "false",
        "EXECUTION_MODE": "PAPER",
        "BLESSING_DISABLE_TEST_DOTENV": "1",
    })
    try:
        result = subprocess.run(
            command, cwd=root, env=env, capture_output=True, text=True, timeout=180, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("LOCAL_PILOT_CI_REGRESSION_FAILED") from exc

    if result.returncode != 0:
        raise RuntimeError("LOCAL_PILOT_CI_REGRESSION_FAILED")
    summaries = [line.strip() for line in result.stdout.splitlines() if " passed" in line]
    match = SUMMARY.fullmatch(summaries[-1]) if summaries else None
    if match is None or int(match.group("count")) != expected:
        raise RuntimeError("LOCAL_PILOT_CI_REGRESSION_NOT_ALL_PASSED")
    return f"{expected} passed"


def run_acceptance(root: Path | None = None) -> dict[str, str]:
    checkout = (root or Path(__file__).resolve().parents[1]).resolve()
    return {name: _run_group(checkout, name, nodes, count) for name, nodes, count in GROUPS}


def main() -> int:
    try:
        results = run_acceptance()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for name, result in results.items():
        print(f"{name}: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
