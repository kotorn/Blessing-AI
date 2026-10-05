from copy import deepcopy

import pytest

from scripts.local_pilot_track_c import review_identity, validate_statement


def fixture():
    return {
        'id': 12, 'name': 'pilot-review',
        'protection_rules': [{'type': 'required_reviewers', 'prevent_self_review': True,
                              'reviewers': [{'type': 'User', 'reviewer': {'id': 42, 'login': 'reviewer'}},
                                            {'type': 'User', 'reviewer': {'id': 43, 'login': 'rev2'}},
                                            {'type': 'User', 'reviewer': {'id': 44, 'login': 'rev3'}}]}],
        'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True},
    }, {'branch_policies': [{'name': 'main', 'type': 'branch'}], 'total_count': 1}, [
        {'state': 'approved', 'environments': [{'id': 12, 'name': 'pilot-review'}],
         'user': {'id': 42, 'login': 'reviewer', 'type': 'User'}, 'comment': 'review'}], {
        'author': {'id': 1, 'login': 'author', 'type': 'User'},
        'committer': {'id': 2, 'login': 'committer', 'type': 'User'},
    }


def test_authenticated_approval_identity():
    assert review_identity(*fixture(), actor_id=3, review_policy={'mode': 'INDEPENDENT', 'operator_github_id': None}) == {'id': 42, 'login': 'reviewer'}


@pytest.mark.parametrize('mutation', ['missing', 'not_allowed', 'branch', 'rejected', 'unknown_author', 'team', 'ambiguous'])
def test_unprovable_review_is_blocked(mutation):
    environment, policies, approvals, commit = deepcopy(fixture())
    if mutation == 'missing': environment['protection_rules'] = []
    if mutation == 'not_allowed': approvals[0]['user']['id'] = 999
    if mutation == 'branch': policies['branch_policies'][0]['name'] = '*'
    if mutation == 'rejected': approvals[0]['state'] = 'rejected'
    if mutation == 'unknown_author': commit['author'] = None
    if mutation == 'team': environment['protection_rules'][0]['reviewers'][0]['type'] = 'Team'
    if mutation == 'ambiguous': approvals.append(deepcopy(approvals[0]))
    with pytest.raises(ValueError):
        review_identity(environment, policies, approvals, commit, actor_id=3, review_policy={'mode': 'INDEPENDENT', 'operator_github_id': None})


def test_independent_mode_self_approval_rejected():
    environment, policies, approvals, commit = deepcopy(fixture())
    with pytest.raises(ValueError, match='REVIEW_INDEPENDENCE_UNPROVEN'):
        review_identity(environment, policies, approvals, commit, actor_id=42,
                        review_policy={'mode': 'INDEPENDENT', 'operator_github_id': None})


def test_independent_mode_author_approval_rejected():
    environment, policies, approvals, commit = deepcopy(fixture())
    commit['author']['id'] = 42
    with pytest.raises(ValueError, match='REVIEW_INDEPENDENCE_UNPROVEN'):
        review_identity(environment, policies, approvals, commit, actor_id=3,
                        review_policy={'mode': 'INDEPENDENT', 'operator_github_id': None})


def test_independent_mode_committer_approval_rejected():
    environment, policies, approvals, commit = deepcopy(fixture())
    commit['committer']['id'] = 42
    with pytest.raises(ValueError, match='REVIEW_INDEPENDENCE_UNPROVEN'):
        review_identity(environment, policies, approvals, commit, actor_id=3,
                        review_policy={'mode': 'INDEPENDENT', 'operator_github_id': None})


def test_independent_mode_requires_prevent_self_review():
    environment, policies, approvals, commit = deepcopy(fixture())
    environment['protection_rules'][0]['prevent_self_review'] = False
    with pytest.raises(ValueError, match='ENVIRONMENT_PROTECTION_UNPROVEN'):
        review_identity(environment, policies, approvals, commit, actor_id=3,
                        review_policy={'mode': 'INDEPENDENT', 'operator_github_id': None})


def test_independent_mode_requires_at_least_three_reviewers():
    environment, policies, approvals, commit = deepcopy(fixture())
    environment['protection_rules'][0]['reviewers'] = [{'type': 'User', 'reviewer': {'id': 42, 'login': 'reviewer'}}]
    with pytest.raises(ValueError, match='ENVIRONMENT_REQUIRED_IDENTITY_UNPROVEN'):
        review_identity(environment, policies, approvals, commit, actor_id=3,
                        review_policy={'mode': 'INDEPENDENT', 'operator_github_id': None})


def test_solo_operator_author_review_accepted():
    environment, policies, approvals, commit = deepcopy(fixture())
    commit['author']['id'] = 42
    environment['protection_rules'][0]['prevent_self_review'] = False
    environment['protection_rules'][0]['reviewers'] = [{'type': 'User', 'reviewer': {'id': 42, 'login': 'reviewer'}}]
    policy = {'mode': 'SOLO_OPERATOR', 'operator_github_id': 42}
    assert review_identity(environment, policies, approvals, commit, actor_id=42, review_policy=policy) == {'id': 42, 'login': 'reviewer'}


def test_solo_operator_wrong_reviewer_rejected():
    environment, policies, approvals, commit = deepcopy(fixture())
    policy = {'mode': 'SOLO_OPERATOR', 'operator_github_id': 999}
    with pytest.raises(ValueError, match='ENVIRONMENT_REQUIRED_IDENTITY_UNPROVEN'):
        review_identity(environment, policies, approvals, commit, actor_id=3, review_policy=policy)


def statement_fixture():
    binding = dict(gitSha='a'*40, sourceSha256='b'*64, dependencySha256='c'*64,
                   migrationSha256='d'*64, pilotPolicySha256='e'*64)
    statement = dict(schemaVersion=1, evidenceClass='CHECKS', repository='kotorn/Blessing-AI',
                     repositoryId='1366161771', workflowRef='kotorn/Blessing-AI/.github/workflows/ci.yml@refs/heads/main',
                     workflowSha='a'*40, ref='refs/heads/main', eventName='push', runId='123',
                     runAttempt=1, observedAt='2026-10-03T00:00:00Z', status='PASS',
                     payload={'checks': [{'id': x, 'status': 'PASS'} for x in (
                         'TYPESCRIPT_TESTS', 'PYTHON_TESTS', 'LINT', 'BUILD',
                         'POSTGRES_17_MIGRATIONS_RESTART', 'LEASE_FENCING', 'PROTECTION_CLOSE')],
                         'job': {'id': 9, 'run_id': 123, 'name': 'build_and_test', 'conclusion': 'success',
                                 'steps': [{'name': x, 'status': 'completed', 'conclusion': 'success'} for x in (
                                     'TypeScript Lint', 'TypeScript Unit Tests', 'TypeScript Build',
                                     'Python Unit Tests (coverage floor 65%)',
                                     'PostgreSQL 17 migration, crash and Worker startup acceptance (no skips)',
                                     'Pilot lease and protection regression (no skips)')]}}, **binding)
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


def test_authenticated_workflow_run_requires_actor_identity():
    from scripts.local_pilot_track_c import validate_run

    statement = {
        'runId': '123', 'runAttempt': 1, 'gitSha': 'a' * 40, 'eventName': 'push',
    }
    run = {
        'id': 123, 'run_attempt': 1, 'head_sha': 'a' * 40, 'head_branch': 'main',
        'event': 'push', 'repository': {'id': 1366161771},
    }
    with pytest.raises(ValueError, match='ATTESTATION_REST_RUN_MISMATCH'):
        validate_run(run, statement)


def test_testnet_payload_requires_its_authenticated_environment_approval():
    from scripts.local_pilot_track_c import validate_payload

    environment = {
        'id': 33, 'name': 'testnet',
        'protection_rules': [{'type': 'required_reviewers', 'prevent_self_review': True,
                              'reviewers': [{'type': 'User', 'reviewer': {'id': 44}}]}],
        'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True},
    }
    policies = {'total_count': 1, 'branch_policies': [{'name': 'main', 'type': 'branch'}]}
    statement = {
        'evidenceClass': 'TESTNET_ETHUSDC', 'runId': '123', 'runAttempt': 1,
        'gitSha': 'a' * 40, 'eventName': 'workflow_dispatch',
        'payload': {
            'trial': {
                'trial_type': 'PROTECTED_ETHUSDC_V1', 'build_sha': 'a' * 40,
                'environment': 'BINANCE_TESTNET', 'symbol': 'ETHUSDC', 'status': 'PASS',
                'protection_status': 'PROTECTED_VERIFIED', 'close_status': 'VERIFIED',
                'close_order_type': 'MARKET', 'close_order_reduce_only': True,
                'reconciliation_status': 'IN_SYNC', 'diff_count': 0, 'entry_fill_count': 1,
                'position_after': [], 'open_orders_after': [], 'open_algo_after': [],
                'entry_client_order_id': 'entry', 'close_client_order_id': 'close',
                'stop_client_algo_id': 'stop', 'target_client_algo_id': 'target',
                'protection_at_close': {
                    'status': 'PROTECTED', 'observed_at': '2026-10-03T00:00:00.000Z',
                    'close_submission_at': '2026-10-03T00:00:00.000Z',
                    'stop': {'algo_id': '101', 'client_algo_id': 'stop',
                             'order_type': 'STOP_MARKET', 'status': 'NEW',
                             'close_position': True, 'reduce_only': False},
                    'target': {'algo_id': '102', 'client_algo_id': 'target',
                               'order_type': 'TAKE_PROFIT_MARKET', 'status': 'NEW',
                               'close_position': True, 'reduce_only': False},
                },
                'account_baseline': {
                    'position_mode': 'ONE_WAY', 'leverage': 5,
                    'nonzero_positions': 0, 'open_orders': 0, 'open_algo_orders': 0,
                },
            },
            'environmentProof': {'environment': environment, 'branchPolicies': policies},
            'run': {
                'id': 123, 'run_attempt': 1, 'head_sha': 'a' * 40, 'head_branch': 'main',
                'event': 'workflow_dispatch', 'repository': {'id': 1366161771},
                    'actor': {'id': 55, 'login': 'workflow-actor', 'type': 'User'},
            },
            'approvals': [{
                'state': 'approved', 'environments': [{'id': 33, 'name': 'testnet'}],
                'user': {'id': 44, 'type': 'User', 'login': 'testnet-reviewer'},
            }],
        },
    }

    validate_payload(statement)
    trial = statement['payload']['trial']
    invalid_timestamp = dict(trial)
    invalid_timestamp['protection_at_close'] = dict(trial['protection_at_close'])
    invalid_timestamp['protection_at_close']['observed_at'] = '2026-02-30T00:00:00.000Z'
    statement['payload']['trial'] = invalid_timestamp
    with pytest.raises(ValueError, match='ATTESTATION_TESTNET_LIFECYCLE_INCOMPLETE'):
        validate_payload(statement)
    stale_at_submission = dict(trial)
    stale_at_submission['protection_at_close'] = dict(trial['protection_at_close'])
    stale_at_submission['protection_at_close']['close_submission_at'] = '2026-10-03T00:00:05.001Z'
    statement['payload']['trial'] = stale_at_submission
    with pytest.raises(ValueError, match='ATTESTATION_TESTNET_LIFECYCLE_INCOMPLETE'):
        validate_payload(statement)
    statement['payload']['trial'] = trial
    missing_close_protection = dict(trial)
    missing_close_protection.pop('protection_at_close')
    statement['payload']['trial'] = missing_close_protection
    with pytest.raises(ValueError, match='ATTESTATION_TESTNET_LIFECYCLE_INCOMPLETE'):
        validate_payload(statement)
    trial['protection_at_close']['stop']['status'] = 'CANCELED'
    statement['payload']['trial'] = trial
    with pytest.raises(ValueError, match='ATTESTATION_TESTNET_LIFECYCLE_INCOMPLETE'):
        validate_payload(statement)
    trial['protection_at_close']['stop']['status'] = 'NEW'
    trial['protection_at_close']['target']['client_algo_id'] = 'other'
    with pytest.raises(ValueError, match='ATTESTATION_TESTNET_LIFECYCLE_INCOMPLETE'):
        validate_payload(statement)
    trial['protection_at_close']['target']['client_algo_id'] = 'target'
    # Test D4: fill-sized reduce-only bracket shape is accepted
    trial['protection_at_close']['stop']['close_position'] = False
    trial['protection_at_close']['stop']['reduce_only'] = True
    trial['protection_at_close']['target']['close_position'] = False
    trial['protection_at_close']['target']['reduce_only'] = True
    validate_payload(statement)

    # Invalid combination: close_position=False and reduce_only=False is rejected
    trial['protection_at_close']['stop']['reduce_only'] = False
    with pytest.raises(ValueError, match='ATTESTATION_TESTNET_LIFECYCLE_INCOMPLETE'):
        validate_payload(statement)
    trial['protection_at_close']['stop']['reduce_only'] = True

    # Restore default fixture
    trial['protection_at_close']['stop']['close_position'] = True
    trial['protection_at_close']['stop']['reduce_only'] = False
    trial['protection_at_close']['target']['close_position'] = True
    trial['protection_at_close']['target']['reduce_only'] = False

    statement['payload']['approvals'] = []
    with pytest.raises(ValueError, match='TESTNET_ENVIRONMENT_APPROVAL_UNPROVEN'):
        validate_payload(statement)

    statement['payload']['approvals'] = [{
        'state': 'approved', 'environments': [{'id': 33, 'name': 'testnet'}],
        'user': {'id': 44, 'type': 'User', 'login': 'testnet-reviewer'},
    }]
    statement['payload']['trial']['account_baseline']['leverage'] = 11
    with pytest.raises(ValueError, match='TESTNET_ACCOUNT_BASELINE_UNPROVEN'):
        validate_payload(statement)


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


def test_shared_phase_contract():
    import json
    from pathlib import Path
    from scripts.local_pilot_track_c import track_c_phases
    cases = json.loads((Path(__file__).parents[1] / 'fixtures/local_pilot_track_c_phases.json').read_text())
    for fixture in cases:
        result = track_c_phases(fixture['passClasses'], fixture['sourceClean'])
        assert (result['status'] == 'READY') == fixture['ready'], fixture['name']
        assert result['canApprove'] == fixture['ready']
        assert result['canStart'] == fixture['ready']
        assert result['prepared']['status'] == 'NOT_RUN'


@pytest.mark.parametrize('mutation', ['payload_missing', 'unknown_field', 'bad_type', 'missing_check', 'wrong_workflow'])
def test_signed_payload_schema_is_required(mutation):
    statement, binding, cert = statement_fixture()
    if mutation == 'payload_missing': statement.pop('payload')
    if mutation == 'unknown_field': statement['canApprove'] = True
    if mutation == 'bad_type': statement['schemaVersion'] = True
    if mutation == 'missing_check': statement['payload']['checks'].pop()
    if mutation == 'wrong_workflow': statement['workflowRef'] = 'attacker/workflow'
    with pytest.raises(ValueError):
        validate_statement(statement, binding, 'CHECKS', cert, now='2026-10-03T00:00:00Z')


def test_dirty_source_never_calls_signature_verifier(monkeypatch, tmp_path):
    from scripts import verify_local_pilot_track_c as verifier
    calls = []
    def dirty(_):
        raise ValueError('LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN')
    monkeypatch.setattr(verifier, 'source_binding', dirty)
    monkeypatch.setattr(verifier, 'verify_class', lambda *args: calls.append(args))
    assert verifier.verify_all(tmp_path)['sourceClean'] is False
    assert calls == []


@pytest.mark.parametrize('same', ['run', 'mixed_reviewers'])
def test_cross_domain_run_replay_and_mixed_reviewers_blocked(monkeypatch, tmp_path, same):
    from scripts import verify_local_pilot_track_c as verifier
    from scripts.local_pilot_track_c import CLASSES
    binding = statement_fixture()[1]
    monkeypatch.setattr(verifier, 'source_binding', lambda _: binding)
    monkeypatch.setattr(verifier, 'read_review_policy', lambda _: {'mode': 'INDEPENDENT', 'operator_github_id': None})
    def verified(_root, _binding, cls, _now, **_kw):
        index = CLASSES.index(cls)
        # mixed_reviewers: 2 reviewers across 3 domains (42, 42, 43) -> size 2, not 3
        reviewer_id = (42 if index <= 2 else 43) if same == 'mixed_reviewers' else 40 + index
        return dict(status='PASS', reason='TRACK_C_ATTESTATION_VERIFIED', evidenceClass=cls,
                    runId='123' if same == 'run' else str(100+index),
                    reviewer={'id': reviewer_id, 'login': 'fixture'})
    monkeypatch.setattr(verifier, 'verify_class', verified)
    result = verifier.verify_all(tmp_path)
    assert all(r['status'] == 'FAIL' for r in result['classes'] if r['evidenceClass'].startswith('REVIEW_'))


def test_solo_operator_distinct_runs_accepted(monkeypatch, tmp_path):
    from scripts import verify_local_pilot_track_c as verifier
    from scripts.local_pilot_track_c import CLASSES
    binding = statement_fixture()[1]
    monkeypatch.setattr(verifier, 'source_binding', lambda _: binding)
    monkeypatch.setattr(verifier, 'read_review_policy', lambda _: {'mode': 'SOLO_OPERATOR', 'operator_github_id': 42})
    def verified(_root, _binding, cls, _now, **_kw):
        index = CLASSES.index(cls)
        return dict(status='PASS', reason='TRACK_C_ATTESTATION_VERIFIED', evidenceClass=cls,
                    runId=str(100+index), reviewer={'id': 42, 'login': 'fixture'})
    monkeypatch.setattr(verifier, 'verify_class', verified)
    result = verifier.verify_all(tmp_path)
    assert all(r['status'] == 'PASS' for r in result['classes'] if r['evidenceClass'].startswith('REVIEW_'))


def test_solo_operator_wrong_reviewer_fails_in_verify_all(monkeypatch, tmp_path):
    from scripts import verify_local_pilot_track_c as verifier
    from scripts.local_pilot_track_c import CLASSES
    binding = statement_fixture()[1]
    monkeypatch.setattr(verifier, 'source_binding', lambda _: binding)
    monkeypatch.setattr(verifier, 'read_review_policy', lambda _: {'mode': 'SOLO_OPERATOR', 'operator_github_id': 42})
    def verified(_root, _binding, cls, _now, **_kw):
        index = CLASSES.index(cls)
        rev_id = 999 if cls == 'REVIEW_PERSISTENCE' else 42
        return dict(status='PASS', reason='TRACK_C_ATTESTATION_VERIFIED', evidenceClass=cls,
                    runId=str(100+index), reviewer={'id': rev_id, 'login': 'fixture'})
    monkeypatch.setattr(verifier, 'verify_class', verified)
    result = verifier.verify_all(tmp_path)
    assert all(r['status'] == 'FAIL' for r in result['classes'] if r['evidenceClass'].startswith('REVIEW_'))


def test_independent_mode_distinct_reviewers_accepted(monkeypatch, tmp_path):
    from scripts import verify_local_pilot_track_c as verifier
    from scripts.local_pilot_track_c import CLASSES
    binding = statement_fixture()[1]
    monkeypatch.setattr(verifier, 'source_binding', lambda _: binding)
    monkeypatch.setattr(verifier, 'read_review_policy', lambda _: {'mode': 'INDEPENDENT', 'operator_github_id': None})
    def verified(_root, _binding, cls, _now, **_kw):
        index = CLASSES.index(cls)
        return dict(status='PASS', reason='TRACK_C_ATTESTATION_VERIFIED', evidenceClass=cls,
                    runId=str(100+index), reviewer={'id': 40+index, 'login': f'fixture{index}'})
    monkeypatch.setattr(verifier, 'verify_class', verified)
    result = verifier.verify_all(tmp_path)
    assert all(r['status'] == 'PASS' for r in result['classes'] if r['evidenceClass'].startswith('REVIEW_'))


def test_source_changes_during_verification_block_all_classes(monkeypatch, tmp_path):
    from scripts import verify_local_pilot_track_c as verifier
    binding = statement_fixture()[1]
    reads = iter([binding, {**binding, 'gitSha': 'f'*40}])
    monkeypatch.setattr(verifier, 'source_binding', lambda _: next(reads))
    monkeypatch.setattr(verifier, 'verify_class', lambda _r, _b, c, _n:
                        dict(status='FAIL', evidenceClass=c))
    result = verifier.verify_all(tmp_path)
    assert result['sourceClean'] is False and result['classes'] == []


def test_python_readiness_uses_signed_classes_not_local_exports(monkeypatch, tmp_path):
    from apps.trading_worker.venues.binance import local_pilot_readiness as readiness
    from scripts.local_pilot_track_c import CLASSES
    sha = 'a'*40
    monkeypatch.setattr(readiness, '_git', lambda _root, *args: sha if args[0] == 'rev-parse' else '')
    monkeypatch.setattr(readiness, 'verify_ci_attestation', lambda *a, **kw: {'status': 'NOT_RUN'})
    monkeypatch.setattr(readiness, 'verify_all', lambda *a, **kw: {
        'sourceClean': True, 'classes': [{'evidenceClass': c, 'status': 'PASS',
                                         'reason': 'TRACK_C_ATTESTATION_VERIFIED'} for c in CLASSES]})
    result = readiness.local_live_pilot_readiness(tmp_path)
    assert result['status'] == 'READY' and result['can_approve'] is True
    assert result['prepared']['status'] == 'NOT_RUN'
    monkeypatch.setattr(readiness, 'verify_all', lambda *a, **kw: {'sourceClean': False, 'classes': []})
    assert readiness.local_live_pilot_readiness(tmp_path)['status'] == 'BLOCKED'
