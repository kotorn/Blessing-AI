"""Code-owned verification of Local Pilot capability evidence.

The Worker independently rechecks the same committed source, check receipts,
review records, and protected Testnet lifecycle artifact as the Control Plane.
Missing, stale, altered, or mismatched evidence always closes the gate.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from apps.trading_worker.local_runtime import worker_identity_token_value
from .local_pilot_verdict import get_active_pilot_verdict, verify_pilot_readiness_verdict
from scripts.verify_local_pilot_ci_attestation import verify_ci_attestation
from scripts.verify_local_pilot_track_c import verify_all
from scripts.local_pilot_track_c import track_c_phases
from scripts.local_pilot_track_c_source import DEPENDENCY_PATHS, SOURCE_PATHS
from scripts.local_pilot_track_c_source import hash_files as hash_track_c_files


EVIDENCE_PATH = Path("artifacts/local-pilot-capability.json")
MAX_AGE_SECONDS = 24 * 60 * 60
MAX_EVIDENCE_BYTES = 65_536
REQUIRED_CHECKS = (
    "TYPESCRIPT_TESTS", "PYTHON_TESTS", "LINT", "BUILD",
    "POSTGRES_17_MIGRATIONS_RESTART", "LEASE_FENCING", "PROTECTION_CLOSE", "TESTNET_E2E",
)
REQUIRED_REVIEW_DOMAINS = ("AUTH_RELEASE", "ORDER_RISK", "PERSISTENCE")
HEX_SHA256 = re.compile(r"^[a-f0-9]{64}$")
GIT_SHA = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fresh(value: Any, now: datetime) -> bool:
    if not isinstance(value, str):
        return False
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if observed.tzinfo is None:
        return False
    age = (now - observed.astimezone(timezone.utc)).total_seconds()
    return -2 <= age <= MAX_AGE_SECONDS


def _git(root: Path, *args: str) -> str | None:
    try:
        allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_NO_REPLACE_OBJECTS"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = "NUL" if os.name == "nt" else "/dev/null"
        result = subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True,
            text=True, timeout=5, shell=False, env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _hash_files(root: Path, paths: tuple[str, ...]) -> str | None:
    """Same tracked-file hashing as Track C (and the TypeScript fingerprint); None means unverifiable."""
    try:
        return hash_track_c_files(root, paths)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _read_json(path: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_EVIDENCE_BYTES:
            return None, None
        parsed = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError):
        return None, None
    return (parsed, raw) if isinstance(parsed, dict) else (None, raw)


def _matching_artifact(root: Path, relative: str, expected_hash: Any) -> dict[str, Any] | None:
    if not isinstance(expected_hash, str) or not HEX_SHA256.fullmatch(expected_hash):
        return None
    value, raw = _read_json(root / relative)
    if value is None or raw is None or _sha256(raw) != expected_hash:
        return None
    return value


def _testnet_trial_passed(root: Path, git_sha: str, expected_hash: Any, now: datetime) -> bool:
    if not isinstance(expected_hash, str) or not HEX_SHA256.fullmatch(expected_hash):
        return False
    artifacts = root / "artifacts"
    try:
        candidates = sorted(artifacts.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return False
    for file in candidates:
        if not file.is_file() or not re.fullmatch(r"testnet-trial-[A-Za-z0-9_-]+\.json", file.name):
            continue
        try:
            modified = datetime.fromtimestamp(file.stat().st_mtime, tz=timezone.utc)
            if not _fresh(modified.isoformat(), now):
                continue
            raw = file.read_bytes()
            if len(raw) > MAX_EVIDENCE_BYTES or _sha256(raw) != expected_hash:
                continue
            trial = json.loads(raw)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(trial, dict):
            continue
        ids = [trial.get(key) for key in (
            "entry_client_order_id", "close_client_order_id",
            "stop_client_algo_id", "target_client_algo_id",
        )]
        if (
            trial.get("trial_type") == "PROTECTED_ETHUSDC_V1"
            and trial.get("build_sha") == git_sha
            and trial.get("environment") == "BINANCE_TESTNET"
            and trial.get("symbol") == "ETHUSDC"
            and trial.get("status") == "PASS"
            and trial.get("protection_status") == "PROTECTED_VERIFIED"
            and trial.get("close_status") == "VERIFIED"
            and trial.get("reconciliation_status") == "IN_SYNC"
            and type(trial.get("diff_count")) is int and trial.get("diff_count") == 0
            and type(trial.get("entry_fill_count")) is int
            and trial["entry_fill_count"] >= 1
            and all(isinstance(trial.get(key), list) and not trial[key] for key in (
                "position_after", "open_orders_after", "open_algo_after",
            ))
            and all(isinstance(value, str) and value for value in ids)
            and len(set(ids)) == len(ids)
        ):
            return True
    return False


def _verify(root: Path, now: datetime) -> list[str]:
    blockers: list[str] = []
    head = _git(root, "rev-parse", "--verify", "HEAD")
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    if not head or not GIT_SHA.fullmatch(head) or status is None or status:
        blockers.append("LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN")

    evidence, _ = _read_json(root / EVIDENCE_PATH)
    if not evidence or type(evidence.get("schemaVersion")) is not int or evidence.get("schemaVersion") != 1:
        blockers.append("LOCAL_PILOT_CAPABILITY_EVIDENCE_MISSING_OR_STALE")
        return blockers

    source_hash = _hash_files(root, SOURCE_PATHS)
    dependency_hash = _hash_files(root, DEPENDENCY_PATHS)
    migration_hash = _hash_files(root, ("infra/postgres/migrations",))
    policy_hash = None
    try:
        _policy_path = root / "config/risk/live_research_pilot.json"
        policy_hash = _sha256(_policy_path.read_bytes())
    except OSError:
        pass
    identity_matches = (
        head is not None and evidence.get("gitSha") == head
        and evidence.get("sourceSha256") == source_hash
        and evidence.get("dependencySha256") == dependency_hash
        and evidence.get("migrationSha256") == migration_hash
        and evidence.get("pilotPolicySha256") == policy_hash
        and all(isinstance(evidence.get(key), str) and HEX_SHA256.fullmatch(evidence[key]) for key in (
            "sourceSha256", "dependencySha256", "migrationSha256", "pilotPolicySha256",
        ))
        and _fresh(evidence.get("observedAt"), now)
    )
    if not identity_matches:
        blockers.append("LOCAL_PILOT_CAPABILITY_EVIDENCE_MISSING_OR_STALE")
        return blockers

    checks = evidence.get("checks")
    check_ids: list[Any] = [check.get("id") for check in checks if isinstance(check, dict)] if isinstance(checks, list) else []
    if not isinstance(checks, list) or len(checks) != len(REQUIRED_CHECKS) or len(set(check_ids)) != len(check_ids):
        blockers.append("LOCAL_PILOT_CAPABILITY_TESTS_NOT_VERIFIED")
    else:
        for check_id in REQUIRED_CHECKS:
            check = next((item for item in checks if isinstance(item, dict) and item.get("id") == check_id), None)
            if not check or check.get("status") != "PASS" or not _fresh(check.get("observedAt"), now):
                blockers.append("LOCAL_PILOT_CAPABILITY_TESTS_NOT_VERIFIED")
                break
            result = _matching_artifact(root, f"artifacts/local-pilot-checks/{check_id}.json", check.get("resultSha256"))
            if (
                not result or result.get("id") != check_id or result.get("gitSha") != head
                or result.get("status") != "PASS"
                or type(result.get("exitCode")) is not int or result.get("exitCode") != 0
                or result.get("observedAt") != check.get("observedAt")
                or not isinstance(result.get("outputSha256"), str)
                or not HEX_SHA256.fullmatch(result["outputSha256"])
                or (check_id == "TESTNET_E2E" and not _testnet_trial_passed(root, head or "", result.get("outputSha256"), now))
            ):
                blockers.append("LOCAL_PILOT_CAPABILITY_TESTS_NOT_VERIFIED")
                break

    reviews = evidence.get("reviews")
    review_ids: list[Any] = [item.get("reviewerId") for item in reviews if isinstance(item, dict)] if isinstance(reviews, list) else []
    if not isinstance(reviews, list) or len(reviews) != len(REQUIRED_REVIEW_DOMAINS) or len(set(review_ids)) != len(review_ids):
        blockers.append("LOCAL_PILOT_INDEPENDENT_REVIEWS_NOT_VERIFIED")
    else:
        for domain in REQUIRED_REVIEW_DOMAINS:
            review = next((item for item in reviews if isinstance(item, dict) and item.get("domain") == domain), None)
            if not review or review.get("status") != "PASS" or not isinstance(review.get("reviewerId"), str) \
                    or not review["reviewerId"].strip() or not _fresh(review.get("observedAt"), now):
                blockers.append("LOCAL_PILOT_INDEPENDENT_REVIEWS_NOT_VERIFIED")
                break
            report = _matching_artifact(root, f"artifacts/local-pilot-reviews/{domain}.json", review.get("reportSha256"))
            if (
                not report or report.get("domain") != domain or report.get("gitSha") != head
                or report.get("reviewerId") != review.get("reviewerId")
                or report.get("status") != "PASS" or report.get("observedAt") != review.get("observedAt")
            ):
                blockers.append("LOCAL_PILOT_INDEPENDENT_REVIEWS_NOT_VERIFIED")
                break
    return blockers


def local_live_pilot_readiness(root: str | Path | None = None, *, now: datetime | None = None) -> dict[str, object]:
    """Verify current local evidence; the default path is the repository root."""
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    active_verdict = get_active_pilot_verdict()
    if active_verdict is not None:
        token = worker_identity_token_value()
        ok, reason, details = verify_pilot_readiness_verdict(active_verdict, token, now=observed_now)
        if ok:
            return {
                "status": "READY",
                "can_approve": True,
                "can_start": True,
                "implementation_ready": {
                    "status": "PASS",
                    "checks": [{"id": c, "status": "PASS", "reason": "TRACK_C_VERDICT_VERIFIED"} for c in REQUIRED_CHECKS],
                },
                "approval_ready": {"status": "PASS", "checks": []},
                "prepared": {
                    "status": "NOT_RUN",
                    "checks": [{
                        "id": "SERVER_OWNED_PREPARATION", "status": "NOT_RUN",
                        "reason": "LOCAL_PILOT_AUTHENTICATED_PREPARATION_EVIDENCE_NOT_AVAILABLE",
                    }],
                },
                "ci_attestation": {"status": "PASS", "reason": "DELEGATED"},
                "provenance": {"local_checks": "DELEGATED", "reviews": "DELEGATED", "testnet": "DELEGATED"},
                "blockers": [],
                "verdict": active_verdict,
            }
        else:
            return {
                "status": "BLOCKED",
                "can_approve": False,
                "can_start": False,
                "implementation_ready": {
                    "status": "FAIL",
                    "checks": [{"id": c, "status": "FAIL", "reason": f"VERDICT_REJECTED_{reason}"} for c in REQUIRED_CHECKS],
                },
                "approval_ready": {"status": "FAIL", "checks": []},
                "prepared": {
                    "status": "NOT_RUN",
                    "checks": [{
                        "id": "SERVER_OWNED_PREPARATION", "status": "NOT_RUN",
                        "reason": "LOCAL_PILOT_AUTHENTICATED_PREPARATION_EVIDENCE_NOT_AVAILABLE",
                    }],
                },
                "ci_attestation": {"status": "FAIL", "reason": reason},
                "provenance": {"local_checks": "UNVERIFIED", "reviews": "UNVERIFIED", "testnet": "UNVERIFIED"},
                "blockers": [f"LOCAL_PILOT_VERDICT_INVALID:{reason}"],
            }

    repo_root = Path(root).resolve() if root is not None else Path(__file__).resolve().parents[4]
    # CI attests only the canonical build, not local DB/Testnet/review receipts.
    # A dirty checkout cannot inherit the attestation for its unchanged HEAD.
    ci_attestation: dict[str, object] = {
        "status": "NOT_RUN", "reason": "CI_ATTESTATION_SOURCE_NOT_VERIFIED",
    }
    head = _git(repo_root, "rev-parse", "--verify", "HEAD")
    if head and GIT_SHA.fullmatch(head) and _git(
        repo_root, "status", "--porcelain", "--untracked-files=all",
    ) == "":
        try:
            ci_attestation = verify_ci_attestation(repo_root, head, now=observed_now)
            if _git(repo_root, "rev-parse", "--verify", "HEAD") != head or _git(
                repo_root, "status", "--porcelain", "--untracked-files=all",
            ) != "":
                ci_attestation = {"status": "FAIL", "reason": "CI_ATTESTATION_SOURCE_CHANGED"}
        except Exception:
            ci_attestation = {"status": "FAIL", "reason": "CI_ATTESTATION_INVALID"}
    # File hashes establish integrity, not identity or provenance. The Worker
    # must not accept self-authored check/review/Testnet JSON as authority.
    # Keep the gate closed until a trusted collector and attestation channel
    # are provisioned and independently verified.
    provenance_blockers = [
        "LOCAL_PILOT_CHECK_PROVENANCE_UNVERIFIED",
        "LOCAL_PILOT_REVIEW_PROVENANCE_UNVERIFIED",
        "LOCAL_PILOT_TESTNET_PROVENANCE_UNVERIFIED",
    ]
    try:
        blockers: list[str] = [*provenance_blockers, *_verify(repo_root, observed_now)]
    except Exception:
        # Unexpected parse/filesystem/tool failures are not evidence of readiness.
        blockers = ["LOCAL_PILOT_RUNTIME_EVIDENCE_NOT_VERIFIED"]
    source_clean = bool(head and GIT_SHA.fullmatch(head) and _git(
        repo_root, 'status', '--porcelain', '--untracked-files=all') == '')
    try:
        track_c: dict[str, Any] = verify_all(repo_root, now=observed_now.isoformat()) if source_clean else {}
        verified_classes: list[Any] = [r['evidenceClass'] for r in track_c.get('classes', [])
                            if r.get('status') == 'PASS' and r.get('reason') == 'TRACK_C_ATTESTATION_VERIFIED']
        source_clean = source_clean and _git(repo_root, 'rev-parse', '--verify', 'HEAD') == head and _git(
            repo_root, 'status', '--porcelain', '--untracked-files=all') == ''
    except Exception:
        verified_classes = []
    phases = track_c_phases(verified_classes, source_clean)
    if phases['status'] == 'READY':
        blockers = []
    else:
        # Unsigned exports remain diagnostic; verified classes alone clear provenance.
        blockers = [b for b in blockers if b not in provenance_blockers] + phases['blockers']
    return {
        "status": phases['status'],
        "can_approve": phases['canApprove'],
        "can_start": phases['canStart'],
        "implementation_ready": phases['implementationReady'],
        "approval_ready": {
            "status": phases['approvalReady']['status'],
            "checks": [{"id": reason, "status": "FAIL", "reason": reason}
                       for reason in phases['blockers'] if 'PROVENANCE' in reason],
        },
        "prepared": {
            "status": "NOT_RUN",
            "checks": [{
                "id": "SERVER_OWNED_PREPARATION", "status": "NOT_RUN",
                "reason": "LOCAL_PILOT_AUTHENTICATED_PREPARATION_EVIDENCE_NOT_AVAILABLE",
            }],
        },
        "ci_attestation": ci_attestation,
        "provenance": {"local_checks": phases['provenance']['localChecks'],
                       "reviews": phases['provenance']['reviews'], "testnet": phases['provenance']['testnet']},
        "blockers": list(dict.fromkeys(blockers)),
    }
