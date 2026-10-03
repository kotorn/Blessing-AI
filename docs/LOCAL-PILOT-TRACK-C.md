# Track C implementation checkpoint

The implementation branch starts at dd56310. The reference prototype remains
read-only. Current protocol code is not wired into readiness yet and cannot
clear the live gate.

Each class needs a distinct signed artifact subject: CHECKS,
REVIEW_AUTH_RELEASE, REVIEW_ORDER_RISK, REVIEW_PERSISTENCE, TESTNET_ETHUSDC.
Subjects bind the source commit, source/dependency/migration/pilot-policy
digests, workflow commit, run ID, attempt, event and observation time. CI_ONLY
does not satisfy any of these classes. Maximum age is 24 hours.

Review identity is derived from the official REST workflow-run approvals
endpoint, not a submitted name, workflow actor, or OIDC workflow identity.
Each review domain must be dispatched in a separate run: approval history
does not identify the job/domain within a run. Three distinct numeric user
IDs are required. A producer must check pilot-review required reviewers,
prevent_self_review and a custom main-only branch policy before signing.
Unresolved commit author/committer IDs and team membership remain blocked.

Official reference:
https://docs.github.com/en/rest/actions/workflow-runs#get-the-review-history-for-a-workflow-run

Operator-provided configuration snapshot: pilot-review returns 404; testnet
has no protection rules and null deployment branch policy. Settings were not
changed. No real signed Track C bundles have been produced or verified in
this worktree. Unit fixtures establish rejection behavior, not real signing
or exchange acceptance.

Remaining implementation: trusted producer workflows and acceptance payload
validation; dirty-source rejection in the shared entry point; TS/Python
readiness integration and shared parity fixtures; complete negative and
focused regression runs. The capability gate stays BLOCKED until these and
real signed evidence pass. No ARM, exchange orders, database mutation or
external GitHub mutation is part of this implementation session.
