"""Track C protocol. Approval identity comes from REST deployment review history.

No actor/input/reviewer-name field can substitute for an authenticated approval.
One dispatch produces one review domain; GitHub approval history has no job ID.
"""
from __future__ import annotations

from datetime import datetime
import re

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


def validate_payload(statement: dict) -> None:
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
        steps = job.get('steps', [])
        if any(len([s for s in steps if s.get('name') == name and
                    s.get('conclusion') == 'success']) != 1 for name in CHECK_STEPS):
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
                                   proof['approvals'], proof['commit'], actor_id=proof['actorId'])
        if payload['reviewer'] != reviewer:
            raise ValueError('ATTESTATION_REVIEW_IDENTITY_INVALID')
    else:
        if set(payload) != {'trial', 'environmentProof', 'run'}:
            raise ValueError('ATTESTATION_TESTNET_PAYLOAD_INVALID')
        validate_run(payload['run'], statement)
        environment_protection(payload['environmentProof']['environment'],
                               payload['environmentProof']['branchPolicies'], 'testnet')
        trial = payload['trial']
        ids = [trial.get(key) for key in ('entry_client_order_id', 'close_client_order_id',
                                         'stop_client_algo_id', 'target_client_algo_id')]
        if (trial.get('trial_type') != 'PROTECTED_ETHUSDC_V1'
                or trial.get('build_sha') != statement['gitSha']
                or trial.get('environment') != 'BINANCE_TESTNET' or trial.get('symbol') != 'ETHUSDC'
                or trial.get('status') != 'PASS' or trial.get('protection_status') != 'PROTECTED_VERIFIED'
                or trial.get('close_status') != 'VERIFIED' or trial.get('reconciliation_status') != 'IN_SYNC'
                or type(trial.get('diff_count')) is not int or trial['diff_count'] != 0
                or type(trial.get('entry_fill_count')) is not int or trial['entry_fill_count'] < 1
                or any(trial.get(key) != [] for key in ('position_after', 'open_orders_after', 'open_algo_after'))
                or any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != 4):
            raise ValueError('ATTESTATION_TESTNET_LIFECYCLE_INCOMPLETE')


def validate_run(run: dict, statement: dict) -> None:
    if (run.get('id') != int(statement['runId']) or run.get('run_attempt') != statement['runAttempt']
            or run.get('head_sha') != statement['gitSha'] or run.get('head_branch') != 'main'
            or run.get('event') != statement['eventName']
            or run.get('repository', {}).get('id') != int(REPOSITORY_ID)):
        raise ValueError('ATTESTATION_REST_RUN_MISMATCH')


def validate_statement(statement: dict, binding: dict, evidence_class: str,
                       certificate: dict, *, now: str) -> None:
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
    validate_payload(statement)


def environment_protection(environment: dict, policies: dict, name: str) -> list:
    if environment.get('name') != name or type(environment.get('id')) is not int:
        raise ValueError('ENVIRONMENT_UNPROVEN')
    rules = [r for r in environment.get('protection_rules', []) if r.get('type') == 'required_reviewers']
    if len(rules) != 1 or rules[0].get('prevent_self_review') is not True:
        raise ValueError('ENVIRONMENT_PROTECTION_UNPROVEN')
    reviewers = rules[0].get('reviewers', [])
    if not reviewers or any(r.get('type') != 'User' or
                            type(r.get('reviewer', {}).get('id')) is not int for r in reviewers):
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
                    commit: dict, *, actor_id: int) -> dict:
    """Reject incomplete/ambiguous API evidence, including unresolved Git authors."""
    reviewers = environment_protection(environment, policies, 'pilot-review')
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
            or user['id'] not in allowed or user['id'] == actor_id
            or user['id'] in {a['id'] for a in authors}
            or not isinstance(user.get('login'), str) or not user['login']):
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
