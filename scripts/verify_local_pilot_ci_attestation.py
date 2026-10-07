"""Verify the canonical GitHub-signed Local Pilot CI subject without a shell."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from datetime import UTC, datetime

REPOSITORY = "kotorn/Blessing-AI"
WORKFLOW = f"{REPOSITORY}/.github/workflows/ci.yml"
REF = "refs/heads/main"
SUBJECT_PATH = "artifacts/local-pilot-ci-evidence.json"
BUNDLE_PATH = "artifacts/local-pilot-ci-attestation.bundle.json"


def _gh_digest(executable: str) -> str:
    return hashlib.sha256(Path(executable).read_bytes()).hexdigest()


def _trusted_gh_digest(executable: str) -> str | None:
    """Local Windows verification must use the signed, installed GitHub CLI."""
    if os.name != "nt" or Path(executable).resolve() != Path(r"C:\Program Files\GitHub CLI\gh.exe"):
        return None
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    environment["BLESSING_VERIFY_GH_PATH"] = executable
    try:
        before = _gh_digest(executable)
        result = subprocess.run([
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference='Stop'; $PSModuleAutoLoadingPreference='None'; "
            "Import-Module 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1'; "
            "$signature=Get-AuthenticodeSignature -LiteralPath $env:BLESSING_VERIFY_GH_PATH; "
            "if ($signature.Status -ne 'Valid' -or "
            "$signature.SignerCertificate.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,$false) -ne 'GitHub, Inc.') { exit 1 }",
        ], env=environment, capture_output=True, timeout=15, check=False, shell=False)
        if result.returncode != 0 or _gh_digest(executable) != before:
            return None
        return before
    except (OSError, subprocess.SubprocessError):
        return None


def verify_ci_attestation(root: Path, expected_sha: str, *, now: datetime | None = None) -> dict:
    """Require a signed subject and certificate bound to the exact tested source."""
    failure = {"status": "FAIL", "reason": "CI_ATTESTATION_INVALID"}
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        return failure
    subject = root.resolve() / SUBJECT_PATH
    bundle = root.resolve() / BUNDLE_PATH
    if not subject.is_file() or not bundle.is_file():
        return {"status": "NOT_RUN", "reason": "CI_ATTESTATION_MISSING"}
    try:
        if subject.stat().st_size > 65_536 or not 0 < bundle.stat().st_size <= 4_194_304:
            return failure
        subject_bytes = subject.read_bytes()
        bundle_bytes = bundle.read_bytes()
        evidence = json.loads(subject_bytes)
        if not isinstance(evidence, dict):
            return failure
        observed = datetime.fromisoformat(evidence["observedAt"].replace("Z", "+00:00"))
        if observed.tzinfo is None:
            return failure
        age = ((now or datetime.now(UTC)) - observed).total_seconds()
        if (
            type(evidence.get("schemaVersion")) is not int
            or evidence.get("schemaVersion") != 1
            or evidence.get("evidenceType") != "LOCAL_PILOT_CI"
            or evidence.get("repository") != REPOSITORY
            or evidence.get("repositoryId") != "1366161771"
            or evidence.get("commitSha") != expected_sha
            or evidence.get("workflowSha") != expected_sha
            or evidence.get("workflowRef") != f"{WORKFLOW}@{REF}"
            or evidence.get("eventName") != "push"
            or evidence.get("ref") != REF
            or evidence.get("checks") != [{"id": "build_and_test", "conclusion": "success"}]
            or not -2 <= age <= 86_400
        ):
            return failure
        gh = shutil.which("gh")
        if not gh:
            return {"status": "NOT_RUN", "reason": "CI_ATTESTATION_VERIFIER_MISSING"}
        gh_digest = _trusted_gh_digest(gh)
        if gh_digest is None:
            return {"status": "FAIL", "reason": "CI_ATTESTATION_VERIFIER_UNTRUSTED"}
        environment = {
            name: value for name, value in os.environ.items()
            if name.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        }
        result = subprocess.run(
            [gh, "attestation", "verify", str(subject), "--bundle", str(bundle),
             "--repo", REPOSITORY, "--signer-workflow", WORKFLOW,
             "--cert-identity", f"https://github.com/{WORKFLOW}@{REF}",
             "--source-ref", REF, "--source-digest", expected_sha,
             "--signer-digest", expected_sha, "--deny-self-hosted-runners",
             "--format", "json"],
            cwd=root.resolve(), env=environment, capture_output=True,
            text=True, timeout=30, check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 4_194_304:
            detail = result.stderr.strip()[:1024] if result.stderr else None
            return {**failure, "stderr": detail, "detail": detail} if detail else failure
        verified = json.loads(result.stdout)
        if not isinstance(verified, list) or not verified:
            return failure
        if _gh_digest(gh) != gh_digest:
            return {"status": "FAIL", "reason": "CI_ATTESTATION_VERIFIER_CHANGED"}
        # Detect files swapped while the external verifier was reading them.
        if subject.read_bytes() != subject_bytes or bundle.read_bytes() != bundle_bytes:
            return failure
        return {
            "status": "PASS", "reason": "CI_ATTESTATION_VERIFIED",
            "gitSha": expected_sha,
            "subjectSha256": hashlib.sha256(subject_bytes).hexdigest(),
            "bundleSha256": hashlib.sha256(bundle_bytes).hexdigest(),
            "scope": "CI_ONLY", "observedAt": evidence["observedAt"],
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        return failure


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    arguments = parser.parse_args()
    outcome = verify_ci_attestation(arguments.root, arguments.sha)
    print(json.dumps(outcome, sort_keys=True))
    raise SystemExit(0 if outcome["status"] == "PASS" else 1)
