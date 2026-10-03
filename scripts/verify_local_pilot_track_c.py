"""Verify five distinct GitHub-signed subjects. Local JSON is never authority."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

# -I execution still imports only the script's explicitly resolved directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_pilot_track_c import CLASSES, REPOSITORY, WORKFLOW, REF, validate_statement
from verify_local_pilot_ci_attestation import _trusted_gh_digest, _gh_digest


def verify_class(root: Path, binding: dict, evidence_class: str, now: str) -> dict:
    failure = {'status': 'FAIL', 'reason': 'TRACK_C_ATTESTATION_INVALID', 'evidenceClass': evidence_class}
    directory = root / 'artifacts/local-pilot-attestations'
    subject = directory / f'{evidence_class}.json'
    bundle = directory / f'{evidence_class}.bundle.json'
    if not subject.is_file() or not bundle.is_file():
        return {**failure, 'status': 'NOT_RUN', 'reason': 'TRACK_C_ATTESTATION_MISSING'}
    try:
        raw, bundle_raw = subject.read_bytes(), bundle.read_bytes()
        if len(raw) > 65536 or not 0 < len(bundle_raw) <= 4194304:
            return failure
        statement = json.loads(raw)
        gh = shutil.which('gh')
        digest = _trusted_gh_digest(gh) if gh else None
        if digest is None:
            return {**failure, 'reason': 'TRACK_C_VERIFIER_UNTRUSTED'}
        environment = {k: v for k, v in os.environ.items()
                       if k.upper() in {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}}
        sha = binding['gitSha']
        result = subprocess.run([
            gh, 'attestation', 'verify', str(subject), '--bundle', str(bundle),
            '--repo', REPOSITORY, '--signer-workflow', WORKFLOW,
            '--cert-identity', f'https://github.com/{WORKFLOW}@{REF}',
            '--source-ref', REF, '--source-digest', sha, '--signer-digest', sha,
            '--deny-self-hosted-runners', '--format', 'json',
        ], cwd=root, env=environment, capture_output=True, text=True, timeout=40,
           check=False, shell=False)
        if result.returncode != 0 or len(result.stdout) > 4194304:
            return failure
        verified = json.loads(result.stdout)
        if not isinstance(verified, list) or len(verified) != 1:
            return failure
        certificate = verified[0]['verificationResult']['signature']['certificate']
        validate_statement(statement, binding, evidence_class, certificate, now=now)
        if (_gh_digest(gh) != digest or subject.read_bytes() != raw or bundle.read_bytes() != bundle_raw):
            return failure
        # Review API facts must be included in the signed producer subject.
        if evidence_class.startswith('REVIEW_'):
            from local_pilot_track_c import review_identity
            proof = statement['reviewProof']
            reviewer = review_identity(proof['environment'], proof['branchPolicies'],
                                       proof['approvals'], proof['commit'], actor_id=proof['actorId'])
            if statement.get('reviewer') != reviewer:
                return failure
        return {'status': 'PASS', 'reason': 'TRACK_C_ATTESTATION_VERIFIED',
                'evidenceClass': evidence_class, 'runId': statement['runId'],
                'reviewer': statement.get('reviewer')}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return failure


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--binding', required=True)
    args = parser.parse_args()
    now = datetime.now(UTC).isoformat()
    results = [verify_class(args.root.resolve(), json.loads(args.binding), c, now) for c in CLASSES]
    reviewers = [r.get('reviewer', {}).get('id') for r in results if r['evidenceClass'].startswith('REVIEW_') and r['status'] == 'PASS']
    if len(reviewers) != 3 or len(set(reviewers)) != 3:
        for result in results:
            if result['evidenceClass'].startswith('REVIEW_'):
                result['status'] = 'FAIL'
                result['reason'] = 'TRACK_C_REVIEW_INDEPENDENCE_UNPROVEN'
    print(json.dumps({'classes': results}, sort_keys=True))
