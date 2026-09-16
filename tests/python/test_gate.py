"""Tests for apps.release_gate.gate (combined release-gate entrypoint).

run_repo_gate is monkeypatched so no real npm/pytest subprocesses are spawned.
Cloud evidence is synthesised via tmp_path fixtures.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import time
from typing import Any

import pytest

from apps.release_gate import gate as gate_module
from apps.release_gate.gate import run_gate


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_REPO_PASS: dict[str, Any] = {
    "checks": [{"id": "npm_ci", "name": "npm ci", "status": "PASS", "message": "OK"}],
    "overall_passed": True,
    "generated_at": "2026-09-15T00:00:00+00:00",
}

_REPO_FAIL: dict[str, Any] = {
    "checks": [{"id": "npm_ci", "name": "npm ci", "status": "FAIL", "message": "error"}],
    "overall_passed": False,
    "generated_at": "2026-09-15T00:00:00+00:00",
}


def _cloud_evidence(
    *,
    age_sec: float = 60,
    overall_passed: bool = True,
    checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a valid cloud evidence dict with generated_at set to `age_sec` ago."""
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age_sec)
    if checks is None:
        checks = [
            {
                "id": "cloud_iam",
                "name": "Cloud Run IAM",
                "status": "PASS" if overall_passed else "FAIL",
                "message": "OK" if overall_passed else "denied",
            }
        ]
    return {
        "checks": checks,
        "overall_passed": overall_passed,
        "generated_at": ts.isoformat(),
    }


def _write_cloud_evidence(
    repo_root: pathlib.Path,
    data: dict[str, Any],
    filename: str = "cloud-gate-20260915T000000Z.json",
) -> pathlib.Path:
    """Write cloud evidence JSON to repo_root/evidence/ and return the path."""
    ev_dir = repo_root / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    path = ev_dir / filename
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Test 1: fresh valid evidence + repo passes → overall_passed True
# ---------------------------------------------------------------------------

def test_both_pass(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate_module, "run_repo_gate", lambda _: _REPO_PASS)

    _write_cloud_evidence(tmp_path, _cloud_evidence(age_sec=60))

    result = run_gate(tmp_path)

    assert result["overall_passed"] is True
    assert result["repo_tier"]["overall_passed"] is True
    assert result["cloud_tier"]["overall_passed"] is True
    assert "checks" in result["repo_tier"]
    assert "checks" in result["cloud_tier"]
    assert "generated_at" in result


# ---------------------------------------------------------------------------
# Test 2: no evidence/ directory → overall_passed False, explains "missing"
# ---------------------------------------------------------------------------

def test_no_evidence_directory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate_module, "run_repo_gate", lambda _: _REPO_PASS)

    # Do NOT create evidence/ at all
    result = run_gate(tmp_path)

    assert result["overall_passed"] is False
    cloud_checks = result["cloud_tier"]["checks"]
    assert len(cloud_checks) == 1
    check = cloud_checks[0]
    assert check["status"] == "FAIL"
    assert "missing" in check["message"].lower()


# ---------------------------------------------------------------------------
# Test 3: stale evidence → overall_passed False, explains "stale" + age
# ---------------------------------------------------------------------------

def test_stale_evidence(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate_module, "run_repo_gate", lambda _: _REPO_PASS)

    stale_age_sec = 7200  # 2 hours old
    limit_sec = 3600       # 1 hour limit
    _write_cloud_evidence(tmp_path, _cloud_evidence(age_sec=stale_age_sec))

    result = run_gate(tmp_path, max_cloud_evidence_age_sec=limit_sec)

    assert result["overall_passed"] is False
    cloud_checks = result["cloud_tier"]["checks"]
    assert len(cloud_checks) == 1
    check = cloud_checks[0]
    assert check["status"] == "FAIL"
    msg = check["message"].lower()
    assert "stale" in msg
    # The actual computed age should appear (as an integer or float)
    # We know it's ~7200s; check that a number >= 7000 appears in the message.
    import re
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", check["message"])]
    assert any(n >= 7000 for n in numbers), (
        f"Expected a number close to {stale_age_sec} in message: {check['message']!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: malformed JSON → overall_passed False, explains "malformed", no crash
# ---------------------------------------------------------------------------

def test_malformed_json(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate_module, "run_repo_gate", lambda _: _REPO_PASS)

    ev_dir = tmp_path / "evidence"
    ev_dir.mkdir()
    (ev_dir / "cloud-gate-bad.json").write_text("{not valid json!!!", encoding="utf-8")

    # Should NOT raise
    result = run_gate(tmp_path)

    assert result["overall_passed"] is False
    cloud_checks = result["cloud_tier"]["checks"]
    assert len(cloud_checks) == 1
    check = cloud_checks[0]
    assert check["status"] == "FAIL"
    assert "malformed" in check["message"].lower()


# ---------------------------------------------------------------------------
# Test 5: repo tier fails, cloud tier passes → overall_passed False
# ---------------------------------------------------------------------------

def test_repo_fails_cloud_passes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate_module, "run_repo_gate", lambda _: _REPO_FAIL)

    _write_cloud_evidence(tmp_path, _cloud_evidence(age_sec=30, overall_passed=True))

    result = run_gate(tmp_path)

    assert result["overall_passed"] is False
    assert result["repo_tier"]["overall_passed"] is False
    assert result["cloud_tier"]["overall_passed"] is True


# ---------------------------------------------------------------------------
# Test 6: multiple cloud-gate-*.json files → picks most recently modified
# ---------------------------------------------------------------------------

def test_picks_most_recently_modified(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate_module, "run_repo_gate", lambda _: _REPO_PASS)

    ev_dir = tmp_path / "evidence"
    ev_dir.mkdir()

    # Write an older file that overall_passed=False
    old_data = _cloud_evidence(age_sec=120, overall_passed=False)
    old_path = ev_dir / "cloud-gate-20260915T000000Z.json"
    old_path.write_text(json.dumps(old_data), encoding="utf-8")

    # Ensure filesystem mtime ordering is detectable
    time.sleep(0.05)

    # Write a newer file that overall_passed=True
    new_data = _cloud_evidence(age_sec=30, overall_passed=True)
    new_path = ev_dir / "cloud-gate-20260915T000100Z.json"
    new_path.write_text(json.dumps(new_data), encoding="utf-8")

    result = run_gate(tmp_path)

    # Should have picked the newest (passing) file
    assert result["overall_passed"] is True
    assert result["cloud_tier"]["overall_passed"] is True
