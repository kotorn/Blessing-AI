"""Tests for apps.release_gate.repo_gate.

All subprocess calls are mocked so the suite is fast, deterministic, and
does not recurse into npm/pytest/git.
"""

from __future__ import annotations

import pathlib
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apps.release_gate.repo_gate import _check_vite_cutover, _is_cutover_true, run_repo_gate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_completed_process(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Return a mock that looks like subprocess.CompletedProcess."""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


_SUCCESS = _make_completed_process(returncode=0)

# IDs of all checks produced by run_repo_gate (order-stable).
_ALL_CHECK_IDS = [
    "npm_ci",
    "npm_lint",
    "npm_test",
    "npm_build",
    "py_compile_trading_worker",
    "py_import_trading_worker",
    "pytest_unit",
    "git_diff_check",
    "vite_data_connect_cutover",
]


def _fake_run_all_pass(*args: Any, **kwargs: Any) -> MagicMock:
    """subprocess.run side-effect: always succeed."""
    return _make_completed_process(returncode=0)


# ---------------------------------------------------------------------------
# Test: _is_cutover_true helper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("false", False),
        ("False", False),
        ("", False),
        (None, False),
        ("1", False),
        ("yes", False),
    ],
)
def test_is_cutover_true(value: str | None, expected: bool) -> None:
    assert _is_cutover_true(value) is expected


# ---------------------------------------------------------------------------
# Test: all checks pass
# ---------------------------------------------------------------------------

def test_all_checks_pass(tmp_path: pathlib.Path) -> None:
    """When every subprocess succeeds, overall_passed must be True."""
    # Create a minimal .env.example so the cutover check can find the file.
    (tmp_path / ".env.example").write_text("VITE_DATA_CONNECT_CUTOVER=false\n")

    with patch("subprocess.run", side_effect=_fake_run_all_pass):
        result = run_repo_gate(tmp_path)

    assert result["overall_passed"] is True
    assert "checks" in result
    assert "generated_at" in result

    check_ids = {c["id"] for c in result["checks"]}
    for expected_id in _ALL_CHECK_IDS:
        assert expected_id in check_ids, f"Missing check id: {expected_id}"

    for check in result["checks"]:
        assert check["status"] == "PASS", f"Expected PASS for {check['id']}, got {check['status']}"


# ---------------------------------------------------------------------------
# Test: one check fails → overall_passed False, others stay PASS
# ---------------------------------------------------------------------------

def test_one_check_fails_overall_fails(tmp_path: pathlib.Path) -> None:
    """Failing npm_build should make overall_passed False while others stay PASS."""
    (tmp_path / ".env.example").write_text("VITE_DATA_CONNECT_CUTOVER=false\n")

    def _side_effect(*args: Any, **kwargs: Any) -> MagicMock:
        cmd: list[str] = args[0] if args else kwargs.get("args", [])
        # Detect the "npm run build" invocation.
        if "build" in cmd:
            return _make_completed_process(
                returncode=1,
                stdout="",
                stderr="Build failed: type error in some module\n",
            )
        return _make_completed_process(returncode=0)

    with patch("subprocess.run", side_effect=_side_effect):
        result = run_repo_gate(tmp_path)

    assert result["overall_passed"] is False

    by_id = {c["id"]: c for c in result["checks"]}

    assert by_id["npm_build"]["status"] == "FAIL"
    assert "Build failed" in by_id["npm_build"]["message"] or by_id["npm_build"]["message"] != "OK"

    # Every other check that didn't fail subprocess-wise should be PASS.
    for check_id, check in by_id.items():
        if check_id == "npm_build":
            continue
        assert check["status"] == "PASS", (
            f"Expected PASS for {check_id!r} but got {check['status']!r}"
        )


# ---------------------------------------------------------------------------
# Test: VITE_DATA_CONNECT_CUTOVER — passing case
# ---------------------------------------------------------------------------

class TestViteCutoverPass:
    """VITE_DATA_CONNECT_CUTOVER is falsy → check passes."""

    def test_env_example_false(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / ".env.example").write_text("VITE_DATA_CONNECT_CUTOVER=false\n")
        result = _check_vite_cutover(tmp_path)
        assert result["status"] == "PASS"
        assert result["id"] == "vite_data_connect_cutover"

    def test_env_example_missing_key(self, tmp_path: pathlib.Path) -> None:
        """No entry at all in .env.example → still PASS (conservative default)."""
        (tmp_path / ".env.example").write_text("SOME_OTHER_KEY=value\n")
        result = _check_vite_cutover(tmp_path)
        assert result["status"] == "PASS"

    def test_env_example_absent(self, tmp_path: pathlib.Path) -> None:
        """No .env.example file at all → PASS."""
        result = _check_vite_cutover(tmp_path)
        assert result["status"] == "PASS"

    def test_process_env_not_set(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VITE_DATA_CONNECT_CUTOVER", raising=False)
        (tmp_path / ".env.example").write_text("VITE_DATA_CONNECT_CUTOVER=false\n")
        result = _check_vite_cutover(tmp_path)
        assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# Test: VITE_DATA_CONNECT_CUTOVER — failing cases
# ---------------------------------------------------------------------------

class TestViteCutoverFail:
    """VITE_DATA_CONNECT_CUTOVER is 'true' → check fails."""

    def test_env_example_true(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VITE_DATA_CONNECT_CUTOVER", raising=False)
        (tmp_path / ".env.example").write_text("VITE_DATA_CONNECT_CUTOVER=true\n")
        result = _check_vite_cutover(tmp_path)
        assert result["status"] == "FAIL"
        assert "VITE_DATA_CONNECT_CUTOVER" in result["message"]

    def test_process_env_true_overrides_example(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Process env 'true' should fail even if .env.example says false."""
        monkeypatch.setenv("VITE_DATA_CONNECT_CUTOVER", "true")
        (tmp_path / ".env.example").write_text("VITE_DATA_CONNECT_CUTOVER=false\n")
        result = _check_vite_cutover(tmp_path)
        assert result["status"] == "FAIL"
        assert "process environment" in result["message"]

    def test_process_env_True_case_insensitive(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VITE_DATA_CONNECT_CUTOVER", "True")
        result = _check_vite_cutover(tmp_path)
        assert result["status"] == "FAIL"

    def test_env_example_True_case_insensitive(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VITE_DATA_CONNECT_CUTOVER", raising=False)
        (tmp_path / ".env.example").write_text("VITE_DATA_CONNECT_CUTOVER=True\n")
        result = _check_vite_cutover(tmp_path)
        assert result["status"] == "FAIL"


# ---------------------------------------------------------------------------
# Test: cutover check FAIL propagates into run_repo_gate overall_passed
# ---------------------------------------------------------------------------

def test_cutover_fail_propagates_to_overall(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the cutover check fails, overall_passed must be False."""
    monkeypatch.setenv("VITE_DATA_CONNECT_CUTOVER", "true")

    with patch("subprocess.run", side_effect=_fake_run_all_pass):
        result = run_repo_gate(tmp_path)

    assert result["overall_passed"] is False

    by_id = {c["id"]: c for c in result["checks"]}
    assert by_id["vite_data_connect_cutover"]["status"] == "FAIL"

    # All subprocess-backed checks should still be PASS.
    for check_id in _ALL_CHECK_IDS:
        if check_id == "vite_data_connect_cutover":
            continue
        assert by_id[check_id]["status"] == "PASS", (
            f"Expected PASS for {check_id!r} but got {by_id[check_id]['status']!r}"
        )


# ---------------------------------------------------------------------------
# Test: evidence dict schema shape
# ---------------------------------------------------------------------------

def test_evidence_dict_schema(tmp_path: pathlib.Path) -> None:
    """Verify the returned dict has the documented stable schema."""
    (tmp_path / ".env.example").write_text("VITE_DATA_CONNECT_CUTOVER=false\n")

    with patch("subprocess.run", side_effect=_fake_run_all_pass):
        result = run_repo_gate(tmp_path)

    assert isinstance(result, dict)
    assert set(result.keys()) == {"checks", "overall_passed", "generated_at"}
    assert isinstance(result["checks"], list)
    assert isinstance(result["overall_passed"], bool)
    assert isinstance(result["generated_at"], str)

    for check in result["checks"]:
        assert set(check.keys()) >= {"id", "name", "status", "message"}
        assert check["status"] in ("PASS", "FAIL")


# ---------------------------------------------------------------------------
# Test: timeout scenario
# ---------------------------------------------------------------------------

def test_subprocess_timeout_marks_fail(tmp_path: pathlib.Path) -> None:
    """A TimeoutExpired exception for a command marks it FAIL."""
    (tmp_path / ".env.example").write_text("VITE_DATA_CONNECT_CUTOVER=false\n")

    call_count = 0

    def _side_effect(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        # Make the first subprocess call time out (npm ci).
        if call_count == 1:
            raise subprocess.TimeoutExpired(cmd=args[0] if args else [], timeout=300)
        return _make_completed_process(returncode=0)

    with patch("subprocess.run", side_effect=_side_effect):
        result = run_repo_gate(tmp_path)

    assert result["overall_passed"] is False
    by_id = {c["id"]: c for c in result["checks"]}
    assert by_id["npm_ci"]["status"] == "FAIL"
    assert "Timed out" in by_id["npm_ci"]["message"]


# ---------------------------------------------------------------------------
# Test: multiple checks fail
# ---------------------------------------------------------------------------

def test_multiple_failures(tmp_path: pathlib.Path) -> None:
    """Two failures → overall_passed False, both checks are FAIL."""
    (tmp_path / ".env.example").write_text("VITE_DATA_CONNECT_CUTOVER=false\n")

    def _side_effect(*args: Any, **kwargs: Any) -> MagicMock:
        cmd: list[str] = args[0] if args else kwargs.get("args", [])
        if "lint" in cmd:
            return _make_completed_process(returncode=2, stderr="TS error")
        if "test" in cmd and "pytest" not in " ".join(cmd):
            return _make_completed_process(returncode=1, stderr="vitest failed")
        return _make_completed_process(returncode=0)

    with patch("subprocess.run", side_effect=_side_effect):
        result = run_repo_gate(tmp_path)

    assert result["overall_passed"] is False
    by_id = {c["id"]: c for c in result["checks"]}
    assert by_id["npm_lint"]["status"] == "FAIL"
    assert by_id["npm_test"]["status"] == "FAIL"
