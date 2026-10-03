from copy import deepcopy

import pytest

from scripts.local_pilot_track_c import review_identity, validate_statement


def fixture():
    return {
        'id': 12, 'name': 'pilot-review',
        'protection_rules': [{'type': 'required_reviewers', 'prevent_self_review': True,
                              'reviewers': [{'type': 'User', 'reviewer': {'id': 42, 'login': 'reviewer'}}]}],
        'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True},
    }, {'branch_policies': [{'name': 'main', 'type': 'branch'}], 'total_count': 1}, [
        {'state': 'approved', 'environments': [{'id': 12, 'name': 'pilot-review'}],
         'user': {'id': 42, 'login': 'reviewer', 'type': 'User'}, 'comment': 'review'}], {
        'author': {'id': 1, 'login': 'author', 'type': 'User'},
        'committer': {'id': 2, 'login': 'committer', 'type': 'User'},
    }


def test_authenticated_approval_identity():
    assert review_identity(*fixture(), actor_id=3) == {'id': 42, 'login': 'reviewer'}


@pytest.mark.parametrize('mutation', ['missing', 'self', 'branch', 'rejected', 'author', 'unknown_author', 'team', 'ambiguous'])
def test_unprovable_review_is_blocked(mutation):
    environment, policies, approvals, commit = deepcopy(fixture())
    if mutation == 'missing': environment['protection_rules'] = []
    if mutation == 'self': environment['protection_rules'][0]['prevent_self_review'] = False
    if mutation == 'branch': policies['branch_policies'][0]['name'] = '*'
    if mutation == 'rejected': approvals[0]['state'] = 'rejected'
    if mutation == 'author': commit['author']['id'] = 42
    if mutation == 'unknown_author': commit['author'] = None
    if mutation == 'team': environment['protection_rules'][0]['reviewers'][0]['type'] = 'Team'
    if mutation == 'ambiguous': approvals.append(deepcopy(approvals[0]))
    with pytest.raises(ValueError):
        review_identity(environment, policies, approvals, commit, actor_id=3)


def statement_fixture():
    binding = dict(gitSha='a'*40, sourceSha256='b'*64, dependencySha256='c'*64,
                   migrationSha256='d'*64, pilotPolicySha256='e'*64)
    statement = dict(schemaVersion=1, evidenceClass='CHECKS', repository='kotorn/Blessing-AI',
                     repositoryId='1366161771', workflowRef='kotorn/Blessing-AI/.github/workflows/local-pilot-track-c.yml@refs/heads/main',
                     workflowSha='a'*40, ref='refs/heads/main', eventName='push', runId='123',
                     runAttempt=1, observedAt='2026-10-03T00:00:00Z', status='PASS', **binding)
    certificate = {'runInvocationURI': 'https://github.com/kotorn/Blessing-AI/actions/runs/123/attempts/1',
                   'githubWorkflowTrigger': 'push'}
    return statement, binding, certificate


@pytest.mark.parametrize('mutation', ['class', 'run', 'event', 'sha', 'stale', 'ci_only', 'hash', 'certificate'])
def test_statement_replay_rejected(mutation):
    statement, binding, cert = statement_fixture()
    if mutation == 'class': statement['evidenceClass'] = 'TESTNET_ETHUSDC'
    if mutation == 'run': statement['runId'] = '456'
    if mutation == 'event': statement['eventName'] = 'pull_request'
    if mutation == 'sha': statement['gitSha'] = 'f'*40
    if mutation == 'stale': statement['observedAt'] = '2026-09-01T00:00:00Z'
    if mutation == 'ci_only': statement['evidenceClass'] = 'LOCAL_PILOT_CI'
    if mutation == 'hash': statement['migrationSha256'] = 'f'*64
    if mutation == 'certificate': cert = {}
    with pytest.raises(ValueError):
        validate_statement(statement, binding, 'CHECKS', cert, now='2026-10-03T00:00:00Z')


def test_bound_statement_passes():
    statement, binding, cert = statement_fixture()
    validate_statement(statement, binding, 'CHECKS', cert, now='2026-10-03T00:00:00Z')


def test_local_json_without_bundle_is_not_authority(tmp_path):
    import json
    from scripts.verify_local_pilot_track_c import verify_class
    statement, binding, _ = statement_fixture()
    directory = tmp_path / 'artifacts/local-pilot-attestations'
    directory.mkdir(parents=True)
    (directory / 'CHECKS.json').write_text(json.dumps(statement))
    assert verify_class(tmp_path, binding, 'CHECKS', '2026-10-03T00:00:00Z')['status'] == 'NOT_RUN'


def test_verifier_failure_never_passes(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    from scripts import verify_local_pilot_track_c as verifier
    statement, binding, _ = statement_fixture()
    directory = tmp_path / 'artifacts/local-pilot-attestations'
    directory.mkdir(parents=True)
    (directory / 'CHECKS.json').write_text(json.dumps(statement))
    (directory / 'CHECKS.bundle.json').write_text('forged')
    monkeypatch.setattr(verifier.shutil, 'which', lambda _: 'fixture-gh')
    monkeypatch.setattr(verifier, '_trusted_gh_digest', lambda _: 'fixture-digest')
    monkeypatch.setattr(verifier.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1, stdout=''))
    assert verifier.verify_class(tmp_path, binding, 'CHECKS', '2026-10-03T00:00:00Z')['status'] == 'FAIL'
