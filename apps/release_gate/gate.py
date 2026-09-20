"""Combined release-gate entrypoint for Blessing AI.

Merges the offline repo-tier evidence (from repo_gate.py) with the most recent
cloud-tier evidence file written by infra/release_gate/cloud_gate.ps1.

Return shape::

    {
        "repo_tier":     dict,   # full output of run_repo_gate()
        "cloud_tier":    dict,   # cloud evidence dict or synthetic FAIL dict
        "overall_passed": bool,  # True only when both tiers pass
        "generated_at":  str,    # UTC ISO-8601 timestamp of this combine
    }

The cloud_tier dict has the same shape as repo_gate's output when real evidence
is present, or a single-check synthetic FAIL dict when evidence is
missing/malformed/stale.

CLI usage::

    python -m apps.release_gate.gate [--repo-root PATH] [--max-cloud-evidence-age-sec N]

Exit code: 0 if overall_passed, 1 otherwise.

# This script is advisory evidence aggregation only; the release action that
# flips MAINNET_LIVE_APPROVED is separate and requires its own authorization.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import pathlib
import sys
from typing import Any

from apps.release_gate.repo_gate import run_repo_gate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYNTHETIC_CHECK_ID = "cloud_evidence"
_SYNTHETIC_CHECK_NAME = "Cloud-tier evidence file"


def _synthetic_fail(reason: str) -> dict[str, Any]:
    """Return a cloud_tier dict containing a single synthetic FAIL check."""
    return {
        "checks": [
            {
                "id": _SYNTHETIC_CHECK_ID,
                "name": _SYNTHETIC_CHECK_NAME,
                "status": "FAIL",
                "message": reason,
            }
        ],
        "overall_passed": False,
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }


def _parse_utc(ts: str) -> datetime.datetime:
    """Parse an ISO-8601 UTC timestamp; treat naive timestamps as UTC."""
    # Handle 'Z' suffix (e.g. "2026-09-15T10:00:00Z")
    ts_clean = ts.strip()
    if ts_clean.endswith("Z"):
        ts_clean = ts_clean[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(ts_clean)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt


def _load_cloud_evidence(
    repo_root: pathlib.Path,
    max_cloud_evidence_age_sec: int,
) -> dict[str, Any]:
    """Find and validate the most recent cloud-gate evidence file.

    Returns a validated cloud evidence dict, or a synthetic FAIL dict
    describing why evidence is missing/malformed/stale.
    """
    evidence_dir = repo_root / "evidence"

    # (c-i) Evidence directory doesn't exist
    if not evidence_dir.exists():
        return _synthetic_fail(
            "Cloud evidence missing: evidence/ directory does not exist in repo root."
        )

    # (c-ii) No matching file
    pattern = str(evidence_dir / "cloud-gate-*.json")
    candidates = glob.glob(pattern)
    if not candidates:
        return _synthetic_fail(
            "Cloud evidence missing: no cloud-gate-*.json file found in evidence/."
        )

    # Pick the most recently modified file
    best = max(candidates, key=lambda p: pathlib.Path(p).stat().st_mtime)
    best_path = pathlib.Path(best)

    # (c-iii) Not valid JSON
    try:
        raw = best_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return _synthetic_fail(
            f"Cloud evidence malformed: could not parse {best_path.name} as JSON. "
            f"Error: {exc}"
        )

    # (c-iv) Wrong shape: must have checks, overall_passed, generated_at
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("checks"), list)
        or not isinstance(data.get("overall_passed"), bool)
        or not isinstance(data.get("generated_at"), str)
    ):
        return _synthetic_fail(
            f"Cloud evidence malformed: {best_path.name} does not have the required "
            "shape {{checks: list, overall_passed: bool, generated_at: str}}."
        )

    # (c-v) Timestamp older than max_cloud_evidence_age_sec
    try:
        generated_at = _parse_utc(data["generated_at"])
    except (ValueError, TypeError) as exc:
        return _synthetic_fail(
            f"Cloud evidence malformed: could not parse generated_at timestamp "
            f"'{data['generated_at']}' in {best_path.name}. Error: {exc}"
        )

    now = datetime.datetime.now(datetime.UTC)
    age_sec = (now - generated_at).total_seconds()
    if age_sec > max_cloud_evidence_age_sec:
        return _synthetic_fail(
            f"Cloud evidence stale: {best_path.name} generated_at is "
            f"{age_sec:.0f}s ago (limit: {max_cloud_evidence_age_sec}s). "
            "Re-run infra/release_gate/cloud_gate.ps1 to refresh."
        )

    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_gate(
    repo_root: pathlib.Path,
    *,
    max_cloud_evidence_age_sec: int = 86400,
) -> dict[str, Any]:
    """Combine repo-tier and cloud-tier release-gate evidence.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    max_cloud_evidence_age_sec:
        Maximum acceptable age (in seconds) of the cloud-gate evidence file.
        Defaults to 86 400 s (24 h).

    Returns
    -------
    dict
        Combined evidence dict; see module docstring for the exact schema.
    """
    # (a) Always run the repo gate (direct import, no subprocess)
    repo_tier = run_repo_gate(repo_root)

    # (b/c/d/e) Load and validate the most recent cloud evidence
    cloud_tier = _load_cloud_evidence(repo_root, max_cloud_evidence_age_sec)

    # overall_passed: both tiers must pass
    overall_passed = bool(
        repo_tier.get("overall_passed")
        and cloud_tier.get("overall_passed")
    )

    return {
        "repo_tier": repo_tier,
        "cloud_tier": cloud_tier,
        "overall_passed": overall_passed,
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.release_gate.gate",
        description="Combine repo-tier and cloud-tier release-gate evidence.",
    )
    parser.add_argument(
        "--repo-root",
        type=pathlib.Path,
        default=None,
        help=(
            "Path to the repository root. Defaults to three levels up from "
            "this file (apps/release_gate/gate.py → repo root)."
        ),
    )
    parser.add_argument(
        "--max-cloud-evidence-age-sec",
        type=int,
        default=86400,
        help="Maximum age in seconds for the cloud-gate evidence file (default: 86400).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:  # noqa: D401
    """CLI entrypoint; returns the process exit code."""
    args = _build_parser().parse_args(argv)

    if args.repo_root is None:
        # Default: three levels up from apps/release_gate/gate.py → repo root
        args.repo_root = pathlib.Path(__file__).resolve().parent.parent.parent

    evidence = run_gate(
        args.repo_root,
        max_cloud_evidence_age_sec=args.max_cloud_evidence_age_sec,
    )
    print(json.dumps(evidence, indent=2))
    return 0 if evidence["overall_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
