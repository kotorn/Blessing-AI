"""Collect authoritative GitHub evidence and create Track C signed subjects."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from scripts.local_pilot_track_c import (
    CHECK_IDS, CHECK_STEPS, REPOSITORY, REPOSITORY_ID, REF,
    environment_protection, review_identity, validate_run, validate_payload,
    workflow_for, read_review_policy,
)
from scripts.local_pilot_track_c_source import source_binding

API_ROOT = "https://api.github.com"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SHA_RE = re.compile(r"[a-f0-9]{40}")


def api_get_json(path: str, token: str) -> object:
    if (not path.startswith("/repos/kotorn/Blessing-AI/") or ".." in path
            or not token or len(path) > 512):
        raise ValueError("TRACK_C_API_REQUEST_INVALID")
    request = Request(
        API_ROOT + path,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "blessing-ai-local-pilot-track-c",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ValueError("TRACK_C_API_READ_FAILED") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("TRACK_C_API_RESPONSE_TOO_LARGE")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("TRACK_C_API_RESPONSE_INVALID") from exc


def verify_environment(name: str, token: str) -> dict:
    if name not in {"pilot-review", "testnet"}:
        raise ValueError("TRACK_C_ENVIRONMENT_INVALID")
    encoded = quote(name, safe="")
    environment = api_get_json(f"/repos/kotorn/Blessing-AI/environments/{encoded}", token)
    policies = api_get_json(
        f"/repos/kotorn/Blessing-AI/environments/{encoded}/deployment-branch-policies?per_page=100",
        token,
    )
    if not isinstance(environment, dict) or not isinstance(policies, dict):
        raise ValueError("TRACK_C_ENVIRONMENT_UNPROVEN")
    environment_protection(environment, policies, name)
    return {"environment": environment, "branchPolicies": policies}


def _workflow_context(context: dict[str, str], evidence_class: str) -> tuple[dict, dict]:
    expected_event = "push" if evidence_class == "CHECKS" else "workflow_dispatch"
    sha = context.get("sha", "")
    run_id = context.get("run_id", "")
    attempt = context.get("run_attempt", "")
    if (context.get("repository") != REPOSITORY
            or context.get("repository_id") != REPOSITORY_ID
            or context.get("ref") != REF or context.get("event") != expected_event
            or not SHA_RE.fullmatch(sha)
            or not re.fullmatch(r"[1-9][0-9]*", run_id)
            or not re.fullmatch(r"[1-9][0-9]*", attempt)):
        raise ValueError("TRACK_C_WORKFLOW_IDENTITY_INVALID")
    return ({
        "repository": REPOSITORY,
        "repositoryId": REPOSITORY_ID,
        "workflowRef": f"{workflow_for(evidence_class)}@{REF}",
        "workflowSha": sha,
        "ref": REF,
        "eventName": expected_event,
        "runId": run_id,
        "runAttempt": int(attempt),
        "gitSha": sha,
    }, {"runId": int(run_id), "attempt": int(attempt), "sha": sha,
        "eventName": expected_event})


def _verified_run(token: str, identity: dict) -> dict:
    run = api_get_json(f"/repos/kotorn/Blessing-AI/actions/runs/{identity['runId']}", token)
    if not isinstance(run, dict):
        raise ValueError("TRACK_C_REST_RUN_UNPROVEN")
    validate_run(run, {"runId": str(identity["runId"]), "runAttempt": identity["attempt"],
                       "gitSha": identity["sha"], "eventName": identity["eventName"]})
    return run


def validate_testnet_runner_job(jobs_response: object, identity: dict) -> dict:
    """Require the real lifecycle step to have completed before attesting its output.

    The attestation producer runs in the same job, so the enclosing job is still
    in_progress at this point. Its preceding runner step, however, must already
    be completed successfully in GitHub's authoritative job record.
    """
    jobs = jobs_response.get("jobs") if isinstance(jobs_response, dict) else None
    if (not isinstance(jobs, list) or jobs_response.get("total_count") != len(jobs)
            or len(jobs) >= 100 or not all(isinstance(job, dict) for job in jobs)):
        raise ValueError("TESTNET_RUNNER_JOB_UNPROVEN")
    matches = [job for job in jobs if job.get("name") == "testnet_trial"]
    if len(matches) != 1:
        raise ValueError("TESTNET_RUNNER_JOB_UNPROVEN")
    job = matches[0]
    steps = job.get("steps")
    if (job.get("run_id") != identity["runId"] or job.get("head_sha") != identity["sha"]
            or job.get("status") not in {"in_progress", "completed"}
            or (job.get("status") == "completed" and job.get("conclusion") != "success")
            or not isinstance(steps, list)):
        raise ValueError("TESTNET_RUNNER_JOB_UNPROVEN")
    runner_steps = [step for step in steps if isinstance(step, dict)
                    and step.get("name") == "Run the protected ETHUSDC Testnet lifecycle with Testnet-only keys"]
    if (len(runner_steps) != 1 or runner_steps[0].get("status") != "completed"
            or runner_steps[0].get("conclusion") != "success"):
        raise ValueError("TESTNET_RUNNER_STEP_UNPROVEN")
    return job


def build_checks_statement(root: Path, context: dict[str, str], token: str) -> dict:
    base, identity = _workflow_context(context, "CHECKS")
    binding = source_binding(root)
    if binding["gitSha"] != identity["sha"]:
        raise ValueError("TRACK_C_SOURCE_SHA_MISMATCH")
    run = _verified_run(token, identity)
    jobs_response = api_get_json(
        f"/repos/kotorn/Blessing-AI/actions/runs/{identity['runId']}/jobs?per_page=100", token,
    )
    if not isinstance(jobs_response, dict) or not isinstance(jobs_response.get("jobs"), list):
        raise ValueError("TRACK_C_CHECKS_UNPROVEN")
    jobs = jobs_response["jobs"]
    if (jobs_response.get("total_count") != len(jobs) or len(jobs) >= 100
            or not all(isinstance(job, dict) for job in jobs)):
        raise ValueError("TRACK_C_CHECKS_UNPROVEN")
    matching = [job for job in jobs if job.get("name") == "build_and_test"]
    if len(matching) != 1:
        raise ValueError("TRACK_C_CHECKS_UNPROVEN")
    job = matching[0]
    steps = job.get("steps")
    if (job.get("status") != "completed" or job.get("conclusion") != "success"
            or job.get("run_id") != identity["runId"] or job.get("head_sha") != identity["sha"]
            or not isinstance(job.get("id"), int) or isinstance(job.get("id"), bool)
            or job["id"] <= 0 or not isinstance(steps, list)):
        raise ValueError("TRACK_C_CHECKS_UNPROVEN")
    for name in CHECK_STEPS:
        named_steps = [step for step in steps if step.get("name") == name]
        if (len(named_steps) != 1 or named_steps[0].get("status") != "completed"
                or named_steps[0].get("conclusion") != "success"):
            raise ValueError("TRACK_C_CHECKS_UNPROVEN")
    rest_job = {"id": job["id"], "name": job["name"], "run_id": job["run_id"],
                "conclusion": job["conclusion"], "steps": steps}
    payload = {
        "checks": [{"id": check_id, "status": "PASS"} for check_id in CHECK_IDS],
        "job": rest_job,
    }
    statement = _statement(base, binding, payload)
    validate_payload(statement)
    if source_binding(root) != binding:
        raise ValueError("TRACK_C_SOURCE_CHANGED")
    return statement


def _build_dispatch_statement(root: Path, context: dict[str, str], token: str,
                              evidence_class: str) -> dict:
    base, identity = _workflow_context(context, evidence_class)
    binding = source_binding(root)
    if binding["gitSha"] != identity["sha"]:
        raise ValueError("TRACK_C_SOURCE_SHA_MISMATCH")
    run = _verified_run(token, identity)
    if evidence_class.startswith("REVIEW_"):
        proof = verify_environment("pilot-review", token)
        approvals = api_get_json(
            f"/repos/kotorn/Blessing-AI/actions/runs/{identity['runId']}/approvals?per_page=100", token,
        )
        commit = api_get_json(f"/repos/kotorn/Blessing-AI/commits/{identity['sha']}", token)
        if not isinstance(approvals, list) or len(approvals) >= 100 or not isinstance(commit, dict):
            raise ValueError("TRACK_C_REVIEW_APPROVALS_UNPROVEN")
        actor_id = run.get("actor", {}).get("id") if isinstance(run.get("actor"), dict) else None
        reviewer = review_identity(proof["environment"], proof["branchPolicies"], approvals,
                                   commit, actor_id=actor_id, review_policy=read_review_policy(root))
        payload = {"reviewProof": {"run": run, "actorId": actor_id, "commit": commit,
                                   "environment": proof["environment"],
                                   "branchPolicies": proof["branchPolicies"],
                                   "approvals": approvals},
                   "reviewer": reviewer, "domain": evidence_class}
    else:
        jobs_response = api_get_json(
            f"/repos/kotorn/Blessing-AI/actions/runs/{identity['runId']}/jobs?per_page=100", token,
        )
        validate_testnet_runner_job(jobs_response, identity)
        proof = verify_environment("testnet", token)
        approvals = api_get_json(
            f"/repos/kotorn/Blessing-AI/actions/runs/{identity['runId']}/approvals?per_page=100", token,
        )
        if not isinstance(approvals, list) or len(approvals) >= 100:
            raise ValueError("TESTNET_ENVIRONMENT_APPROVAL_UNPROVEN")
        artifacts = sorted((root / "artifacts").glob("testnet-trial-*.json"))
        if len(artifacts) != 1:
            raise ValueError("TRACK_C_TESTNET_ARTIFACT_AMBIGUOUS")
        trial_path = artifacts[0]
        resolved = trial_path.resolve()
        if not resolved.is_relative_to((root / "artifacts").resolve()) or resolved.stat().st_size > 65536:
            raise ValueError("TRACK_C_TESTNET_ARTIFACT_INVALID")
        trial = json.loads(resolved.read_text(encoding="utf-8"))
        payload = {"trial": trial, "environmentProof": proof, "run": run,
                   "approvals": approvals}
    statement = _statement(base, binding, payload, evidence_class=evidence_class)
    # The GitHub certificate is validated separately by the local verifier after signing.
    validate_payload(statement, review_policy=read_review_policy(root))
    if source_binding(root) != binding:
        raise ValueError("TRACK_C_SOURCE_CHANGED")
    return statement


def _statement(base: dict, binding: dict, payload: dict, *, evidence_class: str = "CHECKS") -> dict:
    return {
        "schemaVersion": 1,
        "evidenceClass": evidence_class,
        "repository": base["repository"],
        "repositoryId": base["repositoryId"],
        "workflowRef": base["workflowRef"],
        "workflowSha": base["workflowSha"],
        "ref": base["ref"],
        "eventName": base["eventName"],
        "runId": base["runId"],
        "runAttempt": base["runAttempt"],
        "observedAt": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "gitSha": binding["gitSha"],
        "sourceSha256": binding["sourceSha256"],
        "dependencySha256": binding["dependencySha256"],
        "migrationSha256": binding["migrationSha256"],
        "pilotPolicySha256": binding["pilotPolicySha256"],
        "payload": payload,
    }


def write_statement(root: Path, statement: dict) -> Path:
    directory = root / "artifacts/local-pilot-attestations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{statement['evidenceClass']}.json"
    path.write_text(json.dumps(statement, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8")
    return path


def context_from_environment() -> dict[str, str]:
    names = {"repository": "GITHUB_REPOSITORY", "repository_id": "GITHUB_REPOSITORY_ID",
             "sha": "GITHUB_SHA", "ref": "GITHUB_REF", "event": "GITHUB_EVENT_NAME",
             "run_id": "GITHUB_RUN_ID", "run_attempt": "GITHUB_RUN_ATTEMPT"}
    context = {field: os.environ.get(variable, "") for field, variable in names.items()}
    if not all(context.values()):
        raise ValueError("TRACK_C_WORKFLOW_CONTEXT_MISSING")
    return context


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--class", dest="evidence_class", choices=(
        "CHECKS", "REVIEW_AUTH_RELEASE", "REVIEW_ORDER_RISK", "REVIEW_PERSISTENCE",
        "TESTNET_ETHUSDC",
    ))
    parser.add_argument("--preflight-environment", choices=("pilot-review", "testnet"))
    args = parser.parse_args()
    try:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise ValueError("TRACK_C_API_TOKEN_MISSING")
        if bool(args.evidence_class) == bool(args.preflight_environment):
            raise ValueError("TRACK_C_MODE_INVALID")
        if args.preflight_environment:
            context = context_from_environment()
            if (context["repository"] != REPOSITORY or context["repository_id"] != REPOSITORY_ID
                    or context["ref"] != REF or context["event"] != "workflow_dispatch"
                    or not SHA_RE.fullmatch(context["sha"])):
                raise ValueError("TRACK_C_WORKFLOW_IDENTITY_INVALID")
            binding = source_binding(args.root.resolve())
            if binding["gitSha"] != context["sha"]:
                raise ValueError("TRACK_C_SOURCE_SHA_MISMATCH")
            verify_environment(args.preflight_environment, token)
            print("TRACK_C_ENVIRONMENT_PREFLIGHT_PASS")
            return 0
        root = args.root.resolve()
        context = context_from_environment()
        if args.evidence_class == "CHECKS":
            statement = build_checks_statement(root, context, token)
        else:
            statement = _build_dispatch_statement(root, context, token, args.evidence_class)
        write_statement(root, statement)
        print(f"TRACK_C_SUBJECT_CREATED:{args.evidence_class}")
        return 0
    except (OSError, ValueError, KeyError, TypeError, HTTPError, URLError):
        print("TRACK_C_PRODUCER_FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
