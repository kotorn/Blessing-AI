import json
from datetime import UTC, datetime
from types import SimpleNamespace

from scripts import verify_local_pilot_ci_attestation as verifier


def test_ci_attestation_missing_is_not_run(tmp_path):
    assert verifier.verify_ci_attestation(tmp_path, "a" * 40)["status"] == "NOT_RUN"


def test_ci_attestation_enforces_certificate_source_and_signer_sha(monkeypatch, tmp_path):
    sha = "a" * 40
    subject = tmp_path / verifier.SUBJECT_PATH
    subject.parent.mkdir()
    subject.write_text(json.dumps({
        "schemaVersion": 1, "evidenceType": "LOCAL_PILOT_CI",
        "repository": verifier.REPOSITORY, "repositoryId": "1366161771",
        "commitSha": sha, "workflowSha": sha,
        "workflowRef": f"{verifier.WORKFLOW}@{verifier.REF}",
        "eventName": "push", "ref": verifier.REF,
        "checks": [{"id": "build_and_test", "conclusion": "success"}],
        "observedAt": "2026-09-30T00:00:00+00:00",
    }))
    (tmp_path / verifier.BUNDLE_PATH).write_text("signed-bundle-fixture")
    monkeypatch.setattr(verifier.shutil, "which", lambda name: "trusted-gh")
    monkeypatch.setattr(verifier, "_trusted_gh_digest", lambda name: "fixture-binary-digest")
    monkeypatch.setattr(verifier, "_gh_digest", lambda name: "fixture-binary-digest")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='[{"verificationResult":{}}]')

    monkeypatch.setattr(verifier.subprocess, "run", run)
    outcome = verifier.verify_ci_attestation(tmp_path, sha, now=datetime(2026, 9, 30, tzinfo=UTC))
    assert outcome["status"] == "PASS"
    assert outcome["scope"] == "CI_ONLY"
    args, options = calls[0]
    assert args[args.index("--source-digest") + 1] == sha
    assert args[args.index("--signer-digest") + 1] == sha
    assert args[args.index("--cert-identity") + 1] == f"https://github.com/{verifier.WORKFLOW}@{verifier.REF}"
    assert "--deny-self-hosted-runners" in args
    assert options.get("shell", False) is False
    assert "BINANCE_API_SECRET" not in options["env"]
    monkeypatch.setattr(verifier, "_gh_digest", lambda name: "changed-binary-digest")
    assert verifier.verify_ci_attestation(tmp_path, sha, now=datetime(2026, 9, 30, tzinfo=UTC))["reason"] == "CI_ATTESTATION_VERIFIER_CHANGED"
    monkeypatch.setattr(verifier, "_trusted_gh_digest", lambda name: None)
    assert verifier.verify_ci_attestation(tmp_path, sha, now=datetime(2026, 9, 30, tzinfo=UTC))["reason"] == "CI_ATTESTATION_VERIFIER_UNTRUSTED"
    monkeypatch.setattr(verifier, "_trusted_gh_digest", lambda name: "fixture-binary-digest")
    monkeypatch.setattr(verifier.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""))
    assert verifier.verify_ci_attestation(tmp_path, sha, now=datetime(2026, 9, 30, tzinfo=UTC))["status"] == "FAIL"
