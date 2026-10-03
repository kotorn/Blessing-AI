"""Track C protocol. Approval identity comes from REST deployment review history.

No actor/input/reviewer-name field can substitute for an authenticated approval.
One dispatch produces one review domain; GitHub approval history has no job ID.
"""
from __future__ import annotations

from datetime import UTC, datetime
import re

CLASSES = ('CHECKS', 'REVIEW_AUTH_RELEASE', 'REVIEW_ORDER_RISK',
           'REVIEW_PERSISTENCE', 'TESTNET_ETHUSDC')
REPOSITORY = 'kotorn/Blessing-AI'
REPOSITORY_ID = '1366161771'
WORKFLOW = f'{REPOSITORY}/.github/workflows/local-pilot-track-c.yml'
REF = 'refs/heads/main'


def validate_statement(statement: dict, binding: dict, evidence_class: str,
                       certificate: dict, *, now: str) -> None:
    """Certificate fields are accepted only after gh verifies the signed subject."""
    if evidence_class not in CLASSES or statement.get('evidenceClass') != evidence_class:
        raise ValueError('ATTESTATION_CLASS_MISMATCH')
    if (statement.get('schemaVersion') != 1 or statement.get('repository') != REPOSITORY
            or statement.get('repositoryId') != REPOSITORY_ID
            or statement.get('workflowRef') != f'{WORKFLOW}@{REF}'
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


def review_identity(environment: dict, policies: dict, approvals: list,
                    commit: dict, *, actor_id: int) -> dict:
    """Reject incomplete/ambiguous API evidence, including unresolved Git authors."""
    if environment.get('name') != 'pilot-review' or type(environment.get('id')) is not int:
        raise ValueError('REVIEW_ENVIRONMENT_UNPROVEN')
    rules = [r for r in environment.get('protection_rules', [])
             if r.get('type') == 'required_reviewers']
    if len(rules) != 1 or rules[0].get('prevent_self_review') is not True:
        raise ValueError('REVIEW_PROTECTION_UNPROVEN')
    reviewers = rules[0].get('reviewers', [])
    # Team membership is deliberately unsupported until independently verified.
    if not reviewers or any(r.get('type') != 'User' or
                            type(r.get('reviewer', {}).get('id')) is not int for r in reviewers):
        raise ValueError('REVIEW_REQUIRED_IDENTITY_UNPROVEN')
    if environment.get('deployment_branch_policy') != {
        'protected_branches': False, 'custom_branch_policies': True,
    } or policies.get('total_count') != 1 or len(policies.get('branch_policies', [])) != 1:
        raise ValueError('REVIEW_MAIN_POLICY_UNPROVEN')
    policy = policies['branch_policies'][0]
    if policy.get('name') != 'main' or policy.get('type') != 'branch':
        raise ValueError('REVIEW_MAIN_POLICY_UNPROVEN')
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
