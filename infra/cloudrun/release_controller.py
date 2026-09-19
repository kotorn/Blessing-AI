"""Pinned Cloud Build release-controller entrypoint.

This process is deliberately small and deterministic.  It consumes a one-time
approval through the Control Plane, deploys the exact immutable Worker image
with numeric Secret Manager references, and reads the resulting revision back
through both Cloud Run and the Control Plane.  It cannot arm the Worker and it
has no Binance or SQL credential values.

The Cloud Build configuration supplies the controller runtime image by digest
and a SHA-256 for this file.  Local impersonation is not part of this path;
the process uses the attached Cloud Build/Cloud Run Job identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class ControllerError(RuntimeError):
    """A safe, operator-facing release-controller failure."""


_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-fA-F]{64}$")
_URL_RE = re.compile(r"^https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])*$")
_VERSION_RE = re.compile(r"^[1-9][0-9]*$")
_CANDIDATE_RE = re.compile(r"^rc-[0-9a-fA-F-]{36}$")
_APPROVAL_RE = re.compile(r"^approval-[0-9a-fA-F-]{36}$")
_REVISION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$")
_EXPECTED_PROJECT_ID = "gen-lang-client-0730128480"
_EXPECTED_REGION = "asia-southeast1"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ControllerError(f"required release-controller input is missing: {name}")
    return value


def _validate_inputs() -> dict[str, str]:
    values = {
        name: _required(name)
        for name in (
            "CONTROL_PLANE_URL",
            "CANDIDATE_ID",
            "IMAGE_URI",
            "PROJECT_ID",
            "REGION",
            "CLOUD_SQL_PASSWORD_VERSION",
            "BINANCE_API_KEY_VERSION",
            "BINANCE_API_SECRET_VERSION",
            "RELEASE_CONTROLLER_SCRIPT_SHA256",
            "CONTROLLER_IMAGE",
            "RELEASE_CONTROLLER_SERVICE_ACCOUNT",
        )
    }
    control_plane_host = values["CONTROL_PLANE_URL"].removeprefix("https://")
    if (
        len(values["CONTROL_PLANE_URL"]) > 253
        or "." not in control_plane_host
        or not _URL_RE.fullmatch(values["CONTROL_PLANE_URL"])
    ):
        raise ControllerError("CONTROL_PLANE_URL must be a canonical HTTPS service URL")
    if values["PROJECT_ID"] != _EXPECTED_PROJECT_ID:
        raise ControllerError("release controller is pinned to the Blessing AI project")
    if values["REGION"] != _EXPECTED_REGION:
        raise ControllerError("release controller is pinned to the Blessing AI region")
    expected_controller = (
        f"blessing-release-controller@{values['PROJECT_ID']}.iam.gserviceaccount.com"
    )
    if values["RELEASE_CONTROLLER_SERVICE_ACCOUNT"] != expected_controller:
        raise ControllerError("release controller identity is not the dedicated project service account")
    if not _CANDIDATE_RE.fullmatch(values["CANDIDATE_ID"]):
        raise ControllerError("CANDIDATE_ID is invalid")
    if not _DIGEST_RE.fullmatch(values["IMAGE_URI"]):
        raise ControllerError("IMAGE_URI must be an immutable registry digest")
    if not _DIGEST_RE.fullmatch(values["CONTROLLER_IMAGE"]):
        raise ControllerError("CONTROLLER_IMAGE must be an immutable controller image digest")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", values["RELEASE_CONTROLLER_SCRIPT_SHA256"]):
        raise ControllerError("RELEASE_CONTROLLER_SCRIPT_SHA256 must be a SHA-256 digest")
    for name in (
        "CLOUD_SQL_PASSWORD_VERSION",
        "BINANCE_API_KEY_VERSION",
        "BINANCE_API_SECRET_VERSION",
    ):
        if not _VERSION_RE.fullmatch(values[name]):
            raise ControllerError(f"{name} must be a numeric Secret Manager version")
    return values


def _assert_attached_identity(values: dict[str, str]) -> None:
    """Require the build to run as the dedicated Release Controller identity.

    The controller is intentionally unable to impersonate another principal.
    Checking the active gcloud identity before consuming the one-time approval
    prevents a differently attached build identity from reaching the release
    boundary with otherwise valid inputs.
    """
    accounts = _gcloud(
        "auth",
        "list",
        "--filter=status:ACTIVE",
        "--format=value(account)",
    ).splitlines()
    active = {account.strip() for account in accounts if account.strip()}
    expected = values["RELEASE_CONTROLLER_SERVICE_ACCOUNT"]
    if active != {expected}:
        raise ControllerError("active Cloud Build identity is not the dedicated Release Controller")


def _verify_script_digest(expected: str) -> None:
    actual = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if actual.lower() != expected.lower():
        raise ControllerError("release-controller script checksum does not match the approved manifest")


def _gcloud(*args: str, timeout: int = 300) -> str:
    subcmd = " ".join(args[:3])
    try:
        completed = subprocess.run(
            ["gcloud", *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        err_line = (exc.stderr or "").strip().splitlines()[-1] if exc.stderr else "no stderr"
        raise ControllerError(f"Cloud command 'gcloud {subcmd}' failed (code {exc.returncode}): {err_line}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ControllerError(f"Cloud command 'gcloud {subcmd}' failed: {type(exc).__name__}") from exc
    return completed.stdout.strip()


def _identity_token(control_plane_url: str, account: str = "") -> str:
    try:
        token = _gcloud("auth", "print-identity-token", f"--audiences={control_plane_url}")
        if token:
            return token
    except Exception:
        pass

    if account:
        try:
            token = _gcloud("auth", "print-identity-token", account, f"--audiences={control_plane_url}")
            if token:
                return token
        except Exception:
            pass

    try:
        access_token = _gcloud("auth", "print-access-token")
        target_account = account or _gcloud("auth", "list", "--filter=status:ACTIVE", "--format=value(account)")
        url = f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{target_account}:generateIdToken"
        req = urllib.request.Request(
            url,
            data=json.dumps({"audience": control_plane_url, "includeEmail": True}).encode("utf-8"),
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            token = data.get("token", "").strip()
            if token:
                return token
    except Exception as exc:
        raise ControllerError(f"attached Release Controller identity token generation failed: {type(exc).__name__}") from exc

    raise ControllerError("attached Release Controller identity token is empty")


def _post_json(
    control_plane_url: str,
    token: str,
    route: str,
    payload: dict[str, Any],
    *,
    timeout: int = 120,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{control_plane_url}{route}",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(256_000)
    except urllib.error.HTTPError as exc:
        raise ControllerError(f"Control Plane release route returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ControllerError(f"Control Plane release route unavailable: {type(exc).__name__}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerError("Control Plane release route returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ControllerError("Control Plane release route returned a non-object response")
    return value


def _consume_approval(values: dict[str, str], token: str) -> dict[str, Any]:
    verification = _post_json(
        values["CONTROL_PLANE_URL"],
        token,
        "/internal/release/verify",
        {"candidateId": values["CANDIDATE_ID"]},
    )
    if verification.get("verified") is not True or verification.get("evidence_status") != "VERIFIED":
        raise ControllerError("release candidate verification was not independently verified")
    approval = _post_json(
        values["CONTROL_PLANE_URL"],
        token,
        "/internal/release/consume",
        {"candidateId": values["CANDIDATE_ID"]},
    )
    if approval.get("consumed") is not True or approval.get("executionActivated") is True:
        raise ControllerError("release approval was not consumed as an inactive proof")
    if not _APPROVAL_RE.fullmatch(str(approval.get("approvalId", ""))):
        raise ControllerError("Control Plane returned an invalid consumed approval id")
    if approval.get("candidateId") != values["CANDIDATE_ID"]:
        raise ControllerError("consumed approval candidate does not match the requested candidate")
    if approval.get("executionMode") != "LIVE" or approval.get("symbol") != "ETHUSDC":
        raise ControllerError("consumed approval is outside the fixed Mainnet ETHUSDC scope")
    if approval.get("imageDigest") != values["IMAGE_URI"]:
        raise ControllerError("consumed approval image digest does not match the requested image")
    worker_revision = str(approval.get("workerRevision", ""))
    if not _REVISION_RE.fullmatch(worker_revision):
        raise ControllerError("consumed approval worker revision is invalid")
    if approval.get("launchPolicy") != "STAGED_FIRST_ORDER":
        raise ControllerError("consumed approval is not the staged first-order policy")
    if (
        approval.get("viteDataConnectCutover") is not False
        or approval.get("orderSubmissionAttempts") != 0
        or approval.get("workerDisarmed") is not True
    ):
        raise ControllerError("consumed approval is not an unused disarmed release proof")
    versions = approval.get("secretVersions")
    expected_versions = {
        "sql": values["CLOUD_SQL_PASSWORD_VERSION"],
        "apiKey": values["BINANCE_API_KEY_VERSION"],
        "apiSecret": values["BINANCE_API_SECRET_VERSION"],
    }
    if versions != expected_versions:
        raise ControllerError("consumed approval Secret Manager versions do not match the deployment")
    return approval


def _deploy_worker(values: dict[str, str], approval: dict[str, Any]) -> None:
    approval_id = str(approval["approvalId"])
    worker_revision = str(approval["workerRevision"])
    env_vars = ",".join(
        (
            "EXECUTION_MODE=LIVE",
            "MAINNET_LIVE_APPROVED=true",
            "PERSISTENCE_MODE=REQUIRED",
            "EXECUTION_LEASE_REQUIRED=true",
            "MAINNET_LAUNCH_POLICY=STAGED_FIRST_ORDER",
            "MAINNET_MAX_RISK_INCREASING_ORDERS=1",
            "MAINNET_PREFLIGHT_MAX_AGE_SEC=60",
            f"MAINNET_RELEASE_APPROVAL_ID={approval_id}",
            "BINANCE_PORTFOLIO_MARGIN=true",
            f"WORKER_IMAGE_DIGEST={values['IMAGE_URI']}",
            f"WORKER_REVISION={worker_revision}",
            f"CLOUD_SQL_PASSWORD_VERSION={values['CLOUD_SQL_PASSWORD_VERSION']}",
            f"BINANCE_MAINNET_API_KEY_VERSION={values['BINANCE_API_KEY_VERSION']}",
            f"BINANCE_MAINNET_API_SECRET_VERSION={values['BINANCE_API_SECRET_VERSION']}",
            f"POSTGRES_HOST=/cloudsql/{values['PROJECT_ID']}:{values['REGION']}:blessing-sql-primary",
            "POSTGRES_PORT=5432",
            "POSTGRES_DB=blessing_trading",
            "POSTGRES_USER=blessing_worker",
        )
    )
    secret_refs = ",".join(
        (
            f"POSTGRES_PASSWORD=blessing-cloud-sql-password:{values['CLOUD_SQL_PASSWORD_VERSION']}",
            f"BINANCE_MAINNET_API_KEY=blessing-binance-mainnet-api-key:{values['BINANCE_API_KEY_VERSION']}",
            f"BINANCE_MAINNET_API_SECRET=blessing-binance-mainnet-api-secret:{values['BINANCE_API_SECRET_VERSION']}",
        )
    )
    _gcloud(
        "run",
        "deploy",
        "blessing-trading-worker",
        f"--project={values['PROJECT_ID']}",
        f"--region={values['REGION']}",
        "--platform=managed",
        f"--image={values['IMAGE_URI']}",
        f"--service-account=blessing-runtime@{values['PROJECT_ID']}.iam.gserviceaccount.com",
        "--min=1",
        "--max=1",
        "--concurrency=1",
        "--cpu=1",
        "--memory=1Gi",
        "--no-cpu-throttling",
        f"--add-cloudsql-instances={values['PROJECT_ID']}:{values['REGION']}:blessing-sql-primary",
        f"--set-env-vars={env_vars}",
        f"--set-secrets={secret_refs}",
        "--no-allow-unauthenticated",
        "--quiet",
        timeout=900,
    )


def _read_worker_service(
    values: dict[str, str],
    *,
    expected_live_approved: bool,
    expected_approval_id: str,
    expected_source_revision: str,
) -> tuple[dict[str, Any], str]:
    raw = _gcloud(
        "run",
        "services",
        "describe",
        "blessing-trading-worker",
        f"--project={values['PROJECT_ID']}",
        f"--region={values['REGION']}",
        "--format=json",
    )
    try:
        service = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ControllerError("Cloud Run Worker read-back was not valid JSON") from exc
    if not isinstance(service, dict):
        raise ControllerError("Cloud Run Worker read-back was not an object")
    containers = service.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    if not isinstance(containers, list) or not containers:
        raise ControllerError("Cloud Run Worker read-back has no container")
    container = containers[0]
    if not isinstance(container, dict) or container.get("image") != values["IMAGE_URI"]:
        raise ControllerError("Cloud Run Worker image digest read-back does not match")
    template_spec = service.get("spec", {}).get("template", {}).get("spec", {})
    expected_runtime = f"blessing-runtime@{values['PROJECT_ID']}.iam.gserviceaccount.com"
    if template_spec.get("serviceAccountName") != expected_runtime:
        raise ControllerError("Cloud Run Worker runtime service account does not match")
    if template_spec.get("containerConcurrency") != 1:
        raise ControllerError("Cloud Run Worker concurrency must remain 1")
    template_metadata = service.get("spec", {}).get("template", {}).get("metadata", {})
    annotations = template_metadata.get("annotations", {})
    expected_annotations = {
        "autoscaling.knative.dev/minScale": "1",
        "autoscaling.knative.dev/maxScale": "1",
        "run.googleapis.com/cpu-throttling": "false",
        "run.googleapis.com/cloudsql-instances": (
            f"{values['PROJECT_ID']}:{values['REGION']}:blessing-sql-primary"
        ),
    }
    if not isinstance(annotations, dict) or any(
        str(annotations.get(name, "")) != expected for name, expected in expected_annotations.items()
    ):
        raise ControllerError("Cloud Run Worker scaling/CPU/Cloud SQL attachment read-back mismatch")
    expected_env = {
        "EXECUTION_MODE": "LIVE",
        "MAINNET_LIVE_APPROVED": "true" if expected_live_approved else "false",
        "PERSISTENCE_MODE": "REQUIRED",
        "EXECUTION_LEASE_REQUIRED": "true",
        "MAINNET_LAUNCH_POLICY": "STAGED_FIRST_ORDER",
        "MAINNET_MAX_RISK_INCREASING_ORDERS": "1",
        "MAINNET_PREFLIGHT_MAX_AGE_SEC": "60",
        "BINANCE_PORTFOLIO_MARGIN": "true",
        "WORKER_IMAGE_DIGEST": values["IMAGE_URI"],
        "WORKER_REVISION": expected_source_revision,
        "MAINNET_RELEASE_APPROVAL_ID": expected_approval_id,
        "CLOUD_SQL_PASSWORD_VERSION": values["CLOUD_SQL_PASSWORD_VERSION"],
        "BINANCE_MAINNET_API_KEY_VERSION": values["BINANCE_API_KEY_VERSION"],
        "BINANCE_MAINNET_API_SECRET_VERSION": values["BINANCE_API_SECRET_VERSION"],
    }
    env = {
        str(item.get("name")): str(item.get("value"))
        for item in container.get("env", [])
        if isinstance(item, dict) and item.get("name") is not None and item.get("value") is not None
    }
    for name, expected in expected_env.items():
        # The first LIVE-disarmed revision can rely on Cloud Run's immutable
        # K_REVISION. The baseline check below verifies that runtime value;
        # promoted revisions must carry the explicit source revision binding.
        if (
            name in ("WORKER_REVISION", "MAINNET_RELEASE_APPROVAL_ID")
            and not expected_live_approved
            and not env.get(name)
        ):
            continue
        if env.get(name) != expected:
            raise ControllerError(f"Cloud Run Worker environment read-back mismatch: {name}")

    secret_versions = {
        "POSTGRES_PASSWORD": values["CLOUD_SQL_PASSWORD_VERSION"],
        "BINANCE_MAINNET_API_KEY": values["BINANCE_API_KEY_VERSION"],
        "BINANCE_MAINNET_API_SECRET": values["BINANCE_API_SECRET_VERSION"],
    }
    for name, expected_version in secret_versions.items():
        entries = [item for item in container.get("env", []) if item.get("name") == name]
        if len(entries) != 1:
            raise ControllerError(f"Cloud Run Worker secret reference is missing: {name}")
        reference = entries[0].get("valueFrom", {}).get("secretKeyRef", {})
        if reference.get("key") != expected_version:
            raise ControllerError(f"Cloud Run Worker secret version read-back mismatch: {name}")

    status = service.get("status", {})
    latest_ready = status.get("latestReadyRevisionName")
    if not latest_ready:
        raise ControllerError("Cloud Run Worker has no Ready revision")
    conditions = status.get("conditions", [])
    ready_conditions = [
        item for item in conditions
        if isinstance(item, dict) and item.get("type") == "Ready"
    ]
    if not ready_conditions or any(item.get("status") != "True" for item in ready_conditions):
        raise ControllerError("Cloud Run Worker Ready condition is not verified")
    traffic = []
    for item in status.get("traffic", []):
        if not isinstance(item, dict) or item.get("revisionName") != latest_ready:
            continue
        try:
            percent = int(item.get("percent", 0))
        except (TypeError, ValueError) as exc:
            raise ControllerError("Cloud Run Worker traffic read-back is invalid") from exc
        if percent > 0:
            traffic.append(item)
    if not traffic:
        raise ControllerError("Cloud Run Worker Ready revision has no positive traffic")
    return service, str(latest_ready)


def _read_worker_baseline(
    values: dict[str, str], approval: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Verify the disarmed revision named by the candidate before promotion."""

    service, revision = _read_worker_service(
        values,
        expected_live_approved=False,
        expected_approval_id="",
        expected_source_revision=str(approval["workerRevision"]),
    )
    if revision != str(approval["workerRevision"]):
        raise ControllerError(
            "candidate worker revision is not the current Ready disarmed revision"
        )
    return service, revision


def _verify_disarmed_runtime(
    values: dict[str, str], token: str, approval: dict[str, Any]
) -> None:
    runtime = _post_json(values["CONTROL_PLANE_URL"], token, "/internal/release/runtime", {})
    state = runtime.get("state")
    persistence = runtime.get("persistence")
    if not isinstance(state, dict) or not isinstance(persistence, dict):
        raise ControllerError("Control Plane runtime read-back is incomplete")
    expected_state = {
        "executionMode": "LIVE",
        "mainnetLiveApproved": True,
        "engineState": "DISARMED",
        "workerImageDigest": values["IMAGE_URI"],
        "workerRevision": str(approval["workerRevision"]),
        "secretVersions": {
            "sql": values["CLOUD_SQL_PASSWORD_VERSION"],
            "apiKey": values["BINANCE_API_KEY_VERSION"],
            "apiSecret": values["BINANCE_API_SECRET_VERSION"],
        },
        "orderSubmissionAttempts": 0,
        "privateStreamHealthy": False,
        "killSwitchActive": False,
    }
    for name, expected in expected_state.items():
        if state.get(name) != expected:
            raise ControllerError(f"LIVE-approved Worker is not disarmed: {name}")
    if (
        persistence.get("mode") != "REQUIRED"
        or persistence.get("durable") is not True
        or runtime.get("evidence_status") != "VERIFIED"
    ):
        raise ControllerError("LIVE-approved Worker persistence read-back is not durable")


def _verify_control_plane_readiness(values: dict[str, str], token: str) -> None:
    readiness = _post_json(values["CONTROL_PLANE_URL"], token, "/internal/release/readiness", {})
    if readiness.get("status") != "ready" or readiness.get("evidence_status") != "VERIFIED":
        raise ControllerError("Control Plane readiness read-back is degraded")
    checks = readiness.get("checks")
    if not isinstance(checks, list) or not checks or any(
        not isinstance(check, dict) or check.get("status") != "PASS" for check in checks
    ):
        raise ControllerError("Control Plane readiness checks are incomplete")


def main() -> int:
    try:
        values = _validate_inputs()
        _verify_script_digest(values["RELEASE_CONTROLLER_SCRIPT_SHA256"])
        _assert_attached_identity(values)
        token = _identity_token(values["CONTROL_PLANE_URL"], values["RELEASE_CONTROLLER_SERVICE_ACCOUNT"])
        approval = _consume_approval(values, token)
        _, baseline_revision = _read_worker_baseline(values, approval)
        _deploy_worker(values, approval)
        _, promoted_revision = _read_worker_service(
            values,
            expected_live_approved=True,
            expected_approval_id=str(approval["approvalId"]),
            expected_source_revision=str(approval["workerRevision"]),
        )
        if promoted_revision == baseline_revision:
            raise ControllerError("Cloud Run did not create a new promoted Worker revision")
        _verify_control_plane_readiness(values, token)
        _verify_disarmed_runtime(values, token, approval)
        print("Release Controller verified the immutable LIVE-approved DISARMED Worker; no ARM or order was sent")
        return 0
    except ControllerError as exc:
        print(f"Release Controller stopped safely: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - never expose provider payloads
        print(
            f"Release Controller stopped safely: unexpected {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
