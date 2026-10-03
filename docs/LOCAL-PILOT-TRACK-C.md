# Track C implementation checkpoint

The implementation branch starts at dd56310. The reference prototype remains
read-only. Track C is wired into both readiness implementations. A complete
set of verified, schema-valid signed subjects can establish capability
eligibility. Campaign preparation remains NOT_RUN and requires its existing
server-owned approval/preflight flow. Unit fixtures do not establish readiness
for the real checkout.

Each class needs a distinct signed artifact subject: CHECKS,
REVIEW_AUTH_RELEASE, REVIEW_ORDER_RISK, REVIEW_PERSISTENCE, TESTNET_ETHUSDC.
Subjects bind the source commit, source/dependency/migration/pilot-policy
digests, workflow commit, run ID, attempt, event and observation time. CHECKS
uses ci.yml; dispatch review/Testnet classes use local-pilot-track-c.yml. CI_ONLY
does not satisfy any of these classes. Maximum age is 24 hours.

Review identity is derived from the official REST workflow-run approvals
endpoint, not a submitted name, workflow actor, or OIDC workflow identity.
Each review domain must be dispatched in a separate run: approval history
does not identify the job/domain within a run. Three distinct numeric user
IDs are required. A producer must check pilot-review required reviewers,
prevent_self_review and a custom main-only branch policy before signing.
Unresolved commit author/committer IDs and team membership remain blocked.
Review classes also require three distinct run IDs, because deployment
approval history has no job/domain identifier.

The verifier re-computes the clean checkout fingerprint before and after
signature verification. The optional --binding argument is an assertion
against computed values, never authority. Git replacement objects are
disabled. Source fingerprints include the Track C verification scripts and
producer workflow files. Missing/untrusted verifier executables, invalid
certificate run/event claims, stale subjects, changed files, invalid class
payloads, and duplicate reviewers/runs remain blocked. There is no stored
verification-result JSON or injectable production verifier.

CHECKS payloads require the exact seven check IDs and a successful
build_and_test job with the required successful step names. Testnet payloads
require the full protected ETHUSDC lifecycle, no residual position/orders/Algo,
and protected main-only testnet Environment evidence. Reviews require the
signed REST run/environment/branch-policy/approval/commit identity proof.

Official reference:
https://docs.github.com/en/rest/actions/workflow-runs#get-the-review-history-for-a-workflow-run

Operator-provided configuration snapshot: pilot-review returns 404; testnet
has no protection rules and null deployment branch policy; main has no branch
protection. Settings were not
changed. No real signed Track C bundles have been produced or verified in
this worktree. Unit fixtures establish rejection behavior, not real signing
or exchange acceptance.

This checkpoint deliberately stops before installing producer jobs. Those
jobs need a pre-environment REST check so a missing Environment is never
implicitly created by referencing it. The existing CI job also needs an
explicit no-skip lease/protection acceptance step before a CHECKS producer
can sign its schema. A Testnet producer must run the repository's durable
loopback runner on a dedicated PostgreSQL service and capture the actual
trial, not accept a supplied PASS JSON. These are remaining code tasks.

Human prerequisites are to configure main branch protection, pilot-review
required user reviewers/prevent_self_review/main-only branch policy, and the
protected testnet Environment. After authorized delivery of producer code,
three independent non-author users must approve separate review dispatches,
and an authorized Testnet lifecycle dispatch must generate its real bundle.
No such dispatch or configuration change occurred in this worktree.

Focused tests exercise shared complete TS/Python phase outputs, signed-class
readiness wiring with an explicitly mocked cryptographic verifier, malformed
payloads, missing/forged local evidence, wrong binding, dirty-source/change
rejection, class/run/event/SHA replay, freshness, verifier failure and reviewer
independence. These mocks verify gate logic only. No real signed bundle has
confirmed the certificate JSON shape yet, so unsupported output fails closed.
Full-suite/CI/real-bundle acceptance remains NOT_RUN for this slice.
