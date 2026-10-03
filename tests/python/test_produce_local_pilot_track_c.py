from copy import deepcopy

import pytest

from scripts import produce_local_pilot_track_c as producer
from scripts.local_pilot_track_c import CHECK_STEPS, REPOSITORY


def _run():
    return {
        "id": 123, "run_attempt": 2, "head_sha": "a" * 40,
        "head_branch": "main", "event": "push",
        "repository": {"id": 1366161771},
        "actor": {"id": 7, "login": "workflow-actor", "type": "User"},
    }


def _job():
    return {
        "id": 456, "run_id": 123, "name": "build_and_test",
        "status": "completed", "conclusion": "success", "head_sha": "a" * 40,
        "steps": [{"name": name, "status": "completed", "conclusion": "success"}
                  for name in CHECK_STEPS],
    }


def test_checks_statement_is_bound_to_authenticated_rest_job(monkeypatch, tmp_path):
    binding = {
        "gitSha": "a" * 40, "sourceSha256": "b" * 64,
        "dependencySha256": "c" * 64, "migrationSha256": "d" * 64,
        "pilotPolicySha256": "e" * 64,
    }
    monkeypatch.setattr(producer, "source_binding", lambda _root: binding.copy())
    monkeypatch.setattr(producer, "api_get_json", lambda _path, _token: (
        _run() if _path.endswith("/runs/123")
        else {"total_count": 1, "jobs": [_job()]} if _path.endswith("/jobs?per_page=100")
        else None
    ))
    context = {
        "repository": REPOSITORY, "repository_id": "1366161771", "sha": "a" * 40,
        "ref": "refs/heads/main", "event": "push", "run_id": "123", "run_attempt": "2",
    }

    statement = producer.build_checks_statement(tmp_path, context, "token-fixture")

    assert statement["evidenceClass"] == "CHECKS"
    assert statement["payload"]["checks"] == [
        {"id": check_id, "status": "PASS"} for check_id in producer.CHECK_IDS
    ]
    assert statement["payload"]["job"]["id"] == 456
    assert statement["payload"]["job"]["steps"] == _job()["steps"]
    assert statement["workflowRef"] == f"{REPOSITORY}/.github/workflows/ci.yml@refs/heads/main"


def test_checks_statement_rejects_missing_or_duplicate_success_step(monkeypatch, tmp_path):
    monkeypatch.setattr(producer, "source_binding", lambda _root: {
        "gitSha": "a" * 40, "sourceSha256": "b" * 64,
        "dependencySha256": "c" * 64, "migrationSha256": "d" * 64,
        "pilotPolicySha256": "e" * 64,
    })
    bad_job = _job()
    bad_job["steps"].append({
        "name": CHECK_STEPS[0], "status": "completed", "conclusion": "neutral",
    })
    duplicate_run = deepcopy(_run())
    monkeypatch.setattr(producer, "api_get_json", lambda path, _token: (
        duplicate_run if path.endswith("/runs/123") else {"total_count": 1, "jobs": [bad_job]}
    ))
    context = {
        "repository": REPOSITORY, "repository_id": "1366161771", "sha": "a" * 40,
        "ref": "refs/heads/main", "event": "push", "run_id": "123", "run_attempt": "2",
    }

    with pytest.raises(ValueError, match="TRACK_C_CHECKS_UNPROVEN"):
        producer.build_checks_statement(tmp_path, context, "token-fixture")


def test_testnet_attestation_requires_successful_lifecycle_step_from_bound_run():
    identity = {"runId": 123, "sha": "a" * 40}
    job = {
        "id": 789, "run_id": 123, "head_sha": "a" * 40, "name": "testnet_trial",
        # The producer executes later in this job, so only the lifecycle step
        # must have completed; the enclosing job may still be in progress.
        "status": "in_progress", "conclusion": None,
        "steps": [{
            "name": "Run the protected ETHUSDC Testnet lifecycle with Testnet-only keys",
            "status": "completed", "conclusion": "success",
        }],
    }
    assert producer.validate_testnet_runner_job(
        {"total_count": 1, "jobs": [job]}, identity,
    ) == job

    for bad_job in (
        {**job, "status": "completed", "conclusion": "failure"},
        {**job, "head_sha": "b" * 40},
        {**job, "steps": [{**job["steps"][0], "conclusion": "failure"}]},
        {**job, "steps": []},
    ):
        with pytest.raises(ValueError):
            producer.validate_testnet_runner_job(
                {"total_count": 1, "jobs": [bad_job]}, identity,
            )

    with pytest.raises(ValueError, match="TESTNET_RUNNER_JOB_UNPROVEN"):
        producer.validate_testnet_runner_job(
            {"total_count": 2, "jobs": [job]}, identity,
        )


def test_environment_precheck_requires_main_only_required_reviewer(monkeypatch):
    environment = {
        "id": 9, "name": "testnet",
        "protection_rules": [{"type": "required_reviewers", "prevent_self_review": False,
                              "reviewers": [{"type": "User", "reviewer": {"id": 11}}]}],
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
    }
    policies = {"total_count": 1, "branch_policies": [{"name": "main", "type": "branch"}]}
    monkeypatch.setattr(producer, "api_get_json", lambda path, _token: (
        environment if path.endswith("/environments/testnet") else policies
    ))

    with pytest.raises(ValueError, match="ENVIRONMENT_PROTECTION_UNPROVEN"):
        producer.verify_environment("testnet", "token-fixture")


def test_testnet_dispatch_workflow_is_prechecked_and_runs_the_real_runner():
    workflow = producer.Path(producer.__file__).resolve().parents[1] / ".github/workflows/local-pilot-track-c.yml"
    text = workflow.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "TESTNET_ETHUSDC) environment=testnet" in text
    assert "python -m scripts.produce_local_pilot_track_c --root . --preflight-environment \"$environment\"" in text
    assert "testnet_trial:" in text
    assert "needs: preflight" in text
    assert "environment: testnet" in text
    assert "python -m apps.trading_worker.venues.binance.protected_ethusdc_testnet_runner" in text
    assert "MAINNET_LIVE_APPROVED: 'false'" in text
    assert "subject-path: artifacts/local-pilot-attestations/TESTNET_ETHUSDC.json" in text
    assert "--trial" not in text


def test_testnet_workflow_uses_hash_locked_runtime_dependencies():
    root = producer.Path(producer.__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/local-pilot-track-c.yml").read_text(encoding="utf-8")
    lock = (root / "requirements-worker.lock").read_text(encoding="utf-8")
    assert "pip install --require-hashes -r requirements-worker.lock" in workflow
    assert "pip install -e \".[dev]\"" not in workflow
    assert "--hash=sha256:" in lock


def test_pull_request_ci_token_is_read_only_and_not_persisted():
    root = producer.Path(producer.__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "permissions:\n  contents: read\n\njobs:" in workflow
    assert "- uses: actions/checkout@v7\n      with:\n        persist-credentials: false" in workflow
