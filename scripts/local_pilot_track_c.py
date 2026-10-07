"""Track C protocol. Approval identity comes from REST deployment review history.

No actor/input/reviewer-name field can substitute for an authenticated approval.
One dispatch produces one review domain; GitHub approval history has no job ID.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re

REVIEW_POLICY_PATH = 'config/risk/track_c_review_policy.json'


def read_review_policy(root: Path | str | None = None) -> dict:
    policy_path = (Path(root) / REVIEW_POLICY_PATH) if root is not None else Path(REVIEW_POLICY_PATH)
    if not policy_path.exists():
        return {'mode': 'INDEPENDENT', 'operator_github_id': None}
    try:
        data = json.loads(policy_path.read_text(encoding='utf-8'))
        mode = data.get('mode')
        if mode not in {'INDEPENDENT', 'SOLO_OPERATOR'}:
            raise ValueError('REVIEW_POLICY_MODE_INVALID')
        if mode == 'SOLO_OPERATOR':
            op_id = data.get('operator_github_id')
            if type(op_id) is not int:
                raise ValueError('REVIEW_POLICY_OPERATOR_INVALID')
            return {'mode': 'SOLO_OPERATOR', 'operator_github_id': op_id}
        return {'mode': 'INDEPENDENT', 'operator_github_id': None}
    except Exception as e:
        if isinstance(e, ValueError):
            raise
        raise ValueError('REVIEW_POLICY_READ_FAILED') from e

CLASSES = ('CHECKS', 'REVIEW_AUTH_RELEASE', 'REVIEW_ORDER_RISK',
           'REVIEW_PERSISTENCE', 'TESTNET_ETHUSDC')
REPOSITORY = 'kotorn/Blessing-AI'
REPOSITORY_ID = '1366161771'
WORKFLOW = f'{REPOSITORY}/.github/workflows/local-pilot-track-c.yml'
REF = 'refs/heads/main'
CHECK_IDS = ('TYPESCRIPT_TESTS', 'PYTHON_TESTS', 'LINT', 'BUILD',
             'POSTGRES_17_MIGRATIONS_RESTART', 'LEASE_FENCING', 'PROTECTION_CLOSE')
CHECK_STEPS = ('TypeScript Lint', 'TypeScript Unit Tests', 'TypeScript Build',
               'Python Unit Tests (coverage floor 65%)',
               'PostgreSQL 17 migration, crash and Worker startup acceptance (no skips)',
               'Pilot lease and protection regression (no skips)')


def workflow_for(evidence_class: str) -> str:
    return f'{REPOSITORY}/.github/workflows/ci.yml' if evidence_class == 'CHECKS' else WORKFLOW


def _testnet_close_protection_is_proven(trial: dict) -> bool:
    proof = trial.get('protection_at_close')
    if (not isinstance(proof, dict) or set(proof) != {
            'status', 'observed_at', 'close_submission_at', 'stop', 'target',
    } or proof.get('status') != 'PROTECTED'
            or not isinstance(proof.get('observed_at'), str)
            or not isinstance(proof.get('close_submission_at'), str)):
        return False
    timestamp_pattern = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z'
    if any(re.fullmatch(timestamp_pattern, proof[key]) is None
           for key in ('observed_at', 'close_submission_at')):
        return False
    try:
        observed_at = datetime.fromisoformat(proof['observed_at'].replace('Z', '+00:00'))
        close_submission_at = datetime.fromisoformat(
            proof['close_submission_at'].replace('Z', '+00:00')
        )
    except ValueError:
        return False
    if (observed_at.tzinfo is None or close_submission_at.tzinfo is None
            or any(value.strftime('%Y-%m-%dT%H:%M:%S.%f')[:23] + 'Z' != proof[key]
                   for value, key in ((observed_at, 'observed_at'),
                                      (close_submission_at, 'close_submission_at')))
            or not 0 <= (close_submission_at - observed_at).total_seconds() <= 5):
        return False

    filled = trial.get('filled_quantity')
    filled_str = str(filled) if filled is not None else None

    def valid_algo(value: object, expected_client_id: object, expected_type: str) -> bool:
        if not isinstance(value, dict) or set(value) != {
            'algo_id', 'client_algo_id', 'order_type', 'status', 'close_position', 'reduce_only', 'quantity',
        }:
            return False
        algo_id = value.get('algo_id')
        is_reduce_only = value.get('close_position') is False and value.get('reduce_only') is True
        qty = value.get('quantity')
        qty_valid = (
            isinstance(qty, str)
            and re.fullmatch(r'^(?!0(\.0+)?$)\d+(\.\d+)?$', qty) is not None
            and (filled_str is None or qty == filled_str)
        )
        return (isinstance(algo_id, str) and re.fullmatch(r'[1-9][0-9]*', algo_id) is not None
                and value.get('client_algo_id') == expected_client_id
                and value.get('order_type') == expected_type and value.get('status') == 'NEW'
                and is_reduce_only and qty_valid)

    stop = proof.get('stop')
    target = proof.get('target')
    return (valid_algo(stop, trial.get('stop_client_algo_id'), 'STOP_MARKET')
            and valid_algo(target, trial.get('target_client_algo_id'), 'TAKE_PROFIT_MARKET')
            and isinstance(stop, dict) and isinstance(target, dict)
            and stop.get('quantity') == target.get('quantity'))


def validate_payload(statement: dict, *, review_policy: dict | None = None) -> None:
    evidence_class = statement['evidenceClass']
    payload = statement['payload']
    if not isinstance(payload, dict):
        raise ValueError('ATTESTATION_PAYLOAD_INVALID')
    if evidence_class == 'CHECKS':
        if set(payload) != {'checks', 'job'} or payload['checks'] != [
            {'id': x, 'status': 'PASS'} for x in CHECK_IDS
        ]:
            raise ValueError('ATTESTATION_CHECKS_INCOMPLETE')
        job = payload['job']
        if (not isinstance(job, dict) or job.get('name') != 'build_and_test'
                or job.get('conclusion') != 'success' or job.get('run_id') != int(statement['runId'])
                or type(job.get('id')) is not int or job['id'] <= 0):
            raise ValueError('ATTESTATION_CHECK_JOB_INVALID')
        steps = job.get('steps')
        if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
            raise ValueError('ATTESTATION_CHECK_STEPS_INCOMPLETE')
        for name in CHECK_STEPS:
            named = [step for step in steps if step.get('name') == name]
            if (len(named) != 1 or named[0].get('status') != 'completed'
                    or named[0].get('conclusion') != 'success'):
                raise ValueError('ATTESTATION_CHECK_STEPS_INCOMPLETE')
    elif evidence_class.startswith('REVIEW_'):
        if set(payload) != {'reviewProof', 'reviewer', 'domain'} or payload['domain'] != evidence_class:
            raise ValueError('ATTESTATION_REVIEW_PAYLOAD_INVALID')
        proof = payload['reviewProof']
        run = proof['run']
        validate_run(run, statement)
        if proof['actorId'] != run['actor']['id'] or proof['commit'].get('sha') != statement['gitSha']:
            raise ValueError('ATTESTATION_REVIEW_RUN_INVALID')
        reviewer = review_identity(proof['environment'], proof['branchPolicies'],
                                   proof['approvals'], proof['commit'], actor_id=proof['actorId'],
                                   review_policy=review_policy)
        if payload['reviewer'] != reviewer:
            raise ValueError('ATTESTATION_REVIEW_IDENTITY_INVALID')
    else:
        if set(payload) != {'trial', 'environmentProof', 'run', 'approvals'}:
            raise ValueError('ATTESTATION_TESTNET_PAYLOAD_INVALID')
        validate_run(payload['run'], statement)
        proof = payload['environmentProof']
        reviewers = environment_protection(proof['environment'], proof['branchPolicies'], 'testnet')
        approvals = payload['approvals']
        environment_id = proof['environment']['id']
        matching = [approval for approval in approvals if any(
            environment.get('id') == environment_id and environment.get('name') == 'testnet'
            for environment in approval.get('environments', [])
        )] if isinstance(approvals, list) else []
        run_actor = payload['run'].get('actor', {})
        actor_id = run_actor.get('id') if isinstance(run_actor, dict) else None
        user = matching[0].get('user', {}) if len(matching) == 1 else {}
        reviewer_ids = {r['reviewer']['id'] for r in reviewers}
        policy_cfg = review_policy or read_review_policy()
        if (len(matching) != 1 or matching[0].get('state') != 'approved'
                or user.get('type') != 'User' or type(user.get('id')) is not int
                or user['id'] not in reviewer_ids):
            raise ValueError('TESTNET_ENVIRONMENT_APPROVAL_UNPROVEN')
        if policy_cfg['mode'] == 'INDEPENDENT':
            if user['id'] == actor_id:
                raise ValueError('TESTNET_ENVIRONMENT_APPROVAL_UNPROVEN')
        elif policy_cfg['mode'] == 'SOLO_OPERATOR':
            if user['id'] != policy_cfg['operator_github_id']:
                raise ValueError('TESTNET_ENVIRONMENT_APPROVAL_UNPROVEN')
        trial = payload['trial']
        ids = [trial.get(key) for key in ('entry_client_order_id', 'close_client_order_id',
                                         'stop_client_algo_id', 'target_client_algo_id')]
        if (trial.get('trial_type') != 'PROTECTED_ETHUSDC_V1'
                or trial.get('build_sha') != statement['gitSha']
                or trial.get('environment') != 'BINANCE_TESTNET' or trial.get('symbol') != 'ETHUSDC'
                or trial.get('status') != 'PASS' or trial.get('protection_status') != 'PROTECTED_VERIFIED'
                or trial.get('close_status') != 'VERIFIED' or trial.get('reconciliation_status') != 'IN_SYNC'
                or trial.get('close_order_type') != 'MARKET'
                or trial.get('close_order_reduce_only') is not True
                or type(trial.get('diff_count')) is not int or trial['diff_count'] != 0
                or type(trial.get('entry_fill_count')) is not int or trial['entry_fill_count'] < 1
                or not _testnet_close_protection_is_proven(trial)
                or any(trial.get(key) != [] for key in ('position_after', 'open_orders_after', 'open_algo_after'))
                or any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != 4):
            raise ValueError('ATTESTATION_TESTNET_LIFECYCLE_INCOMPLETE')
        baseline = trial.get('account_baseline')
        if (not isinstance(baseline, dict)
                or set(baseline) != {'position_mode', 'leverage', 'nonzero_positions',
                                     'open_orders', 'open_algo_orders'}
                or baseline.get('position_mode') != 'ONE_WAY'
                or type(baseline.get('leverage')) is not int
                or not 1 <= baseline['leverage'] <= 10
                or any(type(baseline.get(key)) is not int or baseline[key] != 0
                       for key in ('nonzero_positions', 'open_orders', 'open_algo_orders'))):
            raise ValueError('TESTNET_ACCOUNT_BASELINE_UNPROVEN')


def validate_run(run: dict, statement: dict) -> None:
    repository = run.get('repository') if isinstance(run, dict) else None
    actor = run.get('actor') if isinstance(run, dict) else None
    if (not isinstance(repository, dict) or type(repository.get('id')) is not int
            or type(run.get('id')) is not int or run['id'] != int(statement['runId'])
            or type(run.get('run_attempt')) is not int
            or run['run_attempt'] != statement['runAttempt']
            or run.get('head_sha') != statement['gitSha'] or run.get('head_branch') != 'main'
            or run.get('event') != statement['eventName']
            or repository['id'] != int(REPOSITORY_ID)
            or not isinstance(actor, dict) or type(actor.get('id')) is not int
            or actor['id'] <= 0 or actor.get('type') != 'User'
            or not isinstance(actor.get('login'), str) or not actor['login']):
        raise ValueError('ATTESTATION_REST_RUN_MISMATCH')


def validate_statement(statement: dict, binding: dict, evidence_class: str,
                       certificate: dict, *, now: str,
                       review_policy: dict | None = None) -> None:
    """Certificate fields are accepted only after gh verifies the signed subject."""
    if not isinstance(statement, dict) or evidence_class not in CLASSES or statement.get('evidenceClass') != evidence_class:
        raise ValueError('ATTESTATION_CLASS_MISMATCH')
    fields = {'schemaVersion', 'evidenceClass', 'repository', 'repositoryId', 'workflowRef',
              'workflowSha', 'ref', 'eventName', 'runId', 'runAttempt', 'observedAt', 'status',
              'gitSha', 'sourceSha256', 'dependencySha256', 'migrationSha256', 'pilotPolicySha256', 'payload'}
    if set(statement) != fields or type(statement.get('schemaVersion')) is not int:
        raise ValueError('ATTESTATION_SCHEMA_INVALID')
    if (statement.get('schemaVersion') != 1 or statement.get('repository') != REPOSITORY
            or statement.get('repositoryId') != REPOSITORY_ID
            or statement.get('workflowRef') != f'{workflow_for(evidence_class)}@{REF}'
            or statement.get('workflowSha') != binding.get('gitSha')
            or statement.get('ref') != REF or statement.get('status') != 'PASS'):
        raise ValueError('ATTESTATION_IDENTITY_MISMATCH')
    for key in ('gitSha', 'sourceSha256', 'dependencySha256', 'migrationSha256', 'pilotPolicySha256'):
        value = statement.get(key)
        if (not isinstance(value, str) or value != binding.get(key)
                or not re.fullmatch(r'[a-f0-9]{40}' if key == 'gitSha' else r'[a-f0-9]{64}', value)):
            raise ValueError('ATTESTATION_BINDING_MISMATCH')
    event = 'push' if evidence_class == 'CHECKS' else 'workflow_dispatch'
    run_id, attempt = statement.get('runId'), statement.get('runAttempt')
    if (not isinstance(run_id, str) or not re.fullmatch(r'[1-9][0-9]*', run_id)
            or type(attempt) is not int or attempt < 1 or statement.get('eventName') != event
            or certificate.get('githubWorkflowTrigger') != event
            or certificate.get('runInvocationURI') !=
            f'https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}'):
        raise ValueError('ATTESTATION_RUN_EVENT_MISMATCH')
    observed = datetime.fromisoformat(statement['observedAt'].replace('Z', '+00:00'))
    current = datetime.fromisoformat(now.replace('Z', '+00:00'))
    if observed.tzinfo is None or current.tzinfo is None or not -2 <= (current-observed).total_seconds() <= 86400:
        raise ValueError('ATTESTATION_STALE')
    validate_payload(statement, review_policy=review_policy)


def environment_protection(environment: dict, policies: dict, name: str,
                           *, review_policy: dict | None = None) -> list:
    if environment.get('name') != name or type(environment.get('id')) is not int:
        raise ValueError('ENVIRONMENT_UNPROVEN')
    rules = [r for r in environment.get('protection_rules', []) if r.get('type') == 'required_reviewers']
    if len(rules) != 1:
        raise ValueError('ENVIRONMENT_PROTECTION_UNPROVEN')
    reviewers = rules[0].get('reviewers', [])
    if not reviewers or any(r.get('type') != 'User' or
                            type(r.get('reviewer', {}).get('id')) is not int for r in reviewers):
        raise ValueError('ENVIRONMENT_REQUIRED_IDENTITY_UNPROVEN')
    policy_cfg = review_policy or read_review_policy()
    if name == 'pilot-review':
        if policy_cfg['mode'] == 'INDEPENDENT':
            if rules[0].get('prevent_self_review') is not True:
                raise ValueError('ENVIRONMENT_PROTECTION_UNPROVEN')
            if len(reviewers) < 3:
                raise ValueError('ENVIRONMENT_REQUIRED_IDENTITY_UNPROVEN')
        elif policy_cfg['mode'] == 'SOLO_OPERATOR':
            op_id = policy_cfg['operator_github_id']
            if not any(r.get('reviewer', {}).get('id') == op_id for r in reviewers):
                raise ValueError('ENVIRONMENT_REQUIRED_IDENTITY_UNPROVEN')
    if environment.get('deployment_branch_policy') != {
        'protected_branches': False, 'custom_branch_policies': True,
    } or policies.get('total_count') != 1 or len(policies.get('branch_policies', [])) != 1:
        raise ValueError('ENVIRONMENT_MAIN_POLICY_UNPROVEN')
    policy = policies['branch_policies'][0]
    if policy.get('name') != 'main' or policy.get('type') != 'branch':
        raise ValueError('ENVIRONMENT_MAIN_POLICY_UNPROVEN')
    return reviewers


def review_identity(environment: dict, policies: dict, approvals: list,
                    commit: dict, *, actor_id: int,
                    review_policy: dict | None = None) -> dict:
    """Reject incomplete/ambiguous API evidence, including unresolved Git authors."""
    policy_cfg = review_policy or read_review_policy()
    reviewers = environment_protection(environment, policies, 'pilot-review', review_policy=policy_cfg)
    authors = [commit.get('author'), commit.get('committer')]
    if any(not isinstance(a, dict) or type(a.get('id')) is not int for a in authors):
        raise ValueError('REVIEW_COMMIT_IDENTITY_UNPROVEN')
    matching = [a for a in approvals if any(
        e.get('id') == environment['id'] and e.get('name') == 'pilot-review'
        for e in a.get('environments', []))]
    if len(matching) != 1 or matching[0].get('state') != 'approved':
        raise ValueError('REVIEW_APPROVAL_UNPROVEN')
    user = matching[0].get('user', {})
    allowed = {r['reviewer']['id'] for r in reviewers}
    if (user.get('type') != 'User' or type(user.get('id')) is not int
            or user['id'] not in allowed
            or not isinstance(user.get('login'), str) or not user['login']):
        raise ValueError('REVIEW_INDEPENDENCE_UNPROVEN')
    if policy_cfg['mode'] == 'INDEPENDENT':
        if user['id'] == actor_id or any(user['id'] == a.get('id') for a in authors):
            raise ValueError('REVIEW_INDEPENDENCE_UNPROVEN')
    elif policy_cfg['mode'] == 'SOLO_OPERATOR':
        if user['id'] != policy_cfg['operator_github_id']:
            raise ValueError('REVIEW_INDEPENDENCE_UNPROVEN')
    return {'id': user['id'], 'login': user['login'].lower()}


def track_c_phases(pass_classes: list[str], source_clean: bool) -> dict:
    """Projection of verifier output only; this function grants no external authority."""
    classes = set(pass_classes) & set(CLASSES) if source_clean else set()
    groups = [('localChecks', ['CHECKS'], 'LOCAL_PILOT_CHECK_PROVENANCE_UNVERIFIED'),
              ('reviews', list(CLASSES[1:4]), 'LOCAL_PILOT_REVIEW_PROVENANCE_UNVERIFIED'),
              ('testnet', ['TESTNET_ETHUSDC'], 'LOCAL_PILOT_TESTNET_PROVENANCE_UNVERIFIED')]
    blockers = [blocker for _, needed, blocker in groups if not set(needed) <= classes]
    if not source_clean:
        blockers.append('LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN')
    checks = [{'id': 'SOURCE_COMMIT', 'status': 'PASS' if source_clean else 'FAIL',
               'reason': 'LOCAL_PILOT_SOURCE_COMMIT_CLEAN' if source_clean else 'LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN'}]
    for check in (*CHECK_IDS, 'TESTNET_E2E'):
        passed = ('TESTNET_ETHUSDC' if check == 'TESTNET_E2E' else 'CHECKS') in classes
        checks.append({'id': check, 'status': 'PASS' if passed else 'NOT_RUN',
                       'reason': 'TRACK_C_ATTESTATION_VERIFIED' if passed else 'LOCAL_PILOT_TRUSTED_CHECK_RUNNER_NOT_AVAILABLE'})
    implementation = 'FAIL' if not source_clean else 'PASS' if all(x['status'] == 'PASS' for x in checks) else 'NOT_RUN'
    return {'status': 'BLOCKED' if blockers else 'READY', 'canApprove': not blockers, 'canStart': not blockers,
            'blockers': blockers, 'provenance': {name: 'VERIFIED' if set(needed) <= classes else 'UNVERIFIED'
                                               for name, needed, _ in groups},
            'implementationReady': {'status': implementation, 'checks': checks},
            'approvalReady': {'status': 'FAIL' if blockers else 'PASS', 'checks': [
                {'id': b, 'status': 'FAIL', 'reason': b} for b in blockers]},
            'prepared': {'status': 'NOT_RUN', 'checks': [{'id': 'SERVER_OWNED_PREPARATION', 'status': 'NOT_RUN',
                 'reason': 'LOCAL_PILOT_AUTHENTICATED_PREPARATION_EVIDENCE_NOT_AVAILABLE'}]}}
