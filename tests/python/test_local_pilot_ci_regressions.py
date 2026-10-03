from types import SimpleNamespace

import pytest

from scripts import run_local_pilot_ci_regressions as runner


def test_ci_regression_groups_are_explicit_and_separate():
    assert [name for name, _nodes, _count in runner.GROUPS] == [
        "LEASE_FENCING",
        "PROTECTION_CLOSE",
    ]
    assert [count for _name, _nodes, count in runner.GROUPS] == [4, 6]
    assert len({node for _name, nodes, _count in runner.GROUPS for node in nodes}) == 9


def test_ci_regression_runner_rejects_skip_even_when_pytest_exits_zero(monkeypatch, tmp_path):
    output = iter(("4 passed in 1.2s\n", "4 passed, 1 skipped in 1.0s\n"))
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=next(output), stderr="")

    monkeypatch.setattr(runner.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="LOCAL_PILOT_CI_REGRESSION_NOT_ALL_PASSED"):
        runner.run_acceptance(tmp_path)

    assert len(calls) == 2
    for args, kwargs in calls:
        assert args[:3] == [runner.sys.executable, "-m", "pytest"]
        assert kwargs["cwd"] == tmp_path
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 180
        assert kwargs["env"]["MAINNET_LIVE_APPROVED"] == "false"
        assert kwargs["env"]["EXECUTION_MODE"] == "PAPER"
        assert "BINANCE_API_SECRET" not in kwargs["env"]


def test_ci_regression_runner_requires_exact_success_count(monkeypatch, tmp_path):
    output = iter(("4 passed in 1.2s\n", "6 passed in 2.3s\n"))
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=next(output), stderr=""),
    )

    assert runner.run_acceptance(tmp_path) == {
        "LEASE_FENCING": "4 passed",
        "PROTECTION_CLOSE": "6 passed",
    }


def test_ci_workflow_runs_the_contract_step():
    workflow = runner.Path(runner.__file__).resolve().parents[1] / ".github/workflows/ci.yml"
    text = workflow.read_text(encoding="utf-8")
    assert "- name: Pilot lease and protection regression (no skips)" in text
    assert "python -m scripts.run_local_pilot_ci_regressions" in text


def test_ci_regression_runner_fails_on_process_error_without_echoing_output(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2, stdout="private test output", stderr="private stderr",
        ),
    )

    with pytest.raises(RuntimeError, match="LOCAL_PILOT_CI_REGRESSION_FAILED") as exc:
        runner.run_acceptance(tmp_path)
    assert "private" not in str(exc.value)
