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
does not identify the job/domain within a run. The review policy is
explicitly configured in `config/risk/track_c_review_policy.json` (hashed in
`pilotPolicySha256`). In `SOLO_OPERATOR` mode, approvals must match the
designated `operator_github_id` across three distinct runs. In `INDEPENDENT`
mode, three distinct numeric user IDs are required, and the producer enforces
`prevent_self_review`, non-author, and non-dispatcher checks. Review classes
always require three distinct run IDs because deployment approval history has
no job/domain identifier.

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

Read-only GitHub check on 2026-10-08: `pilot-review` and `testnet` exist, each with one required reviewer (the operator, id 12929483), a `main`-only custom branch policy and admin bypass enabled; `main` requires 1 review and the `build_and_test` check. No `testnet` secrets are configured yet. Settings were not
changed. No real signed Track C bundles have been produced or verified in
this worktree. Unit fixtures establish rejection behavior, not real signing
or exchange acceptance.

The CI workflow now has an explicit no-skip lease/protection acceptance step.
It runs nine named cases in two independent invocations and fails unless the
exact expected pass count is reported; skipped, xfailed, deselected, warned,
or otherwise unexpected summaries do not produce evidence. Its subprocess
gets only an OS-path allowlist plus PAPER/DISARMED environment values, not
Binance or cloud credentials. This is unit-level lease/order evidence; the
separate PostgreSQL migration/restart acceptance remains authoritative for
database behavior.

The CHECKS producer is wired into the canonical main-push attestation job.
The separate `local-pilot-track-c.yml` workflow supports one review domain or
the protected Testnet trial per dispatch. Its first job reads the Environment
and main-only branch policy through GitHub REST and fails before any protected
job can reference a missing/unprotected Environment. Review subjects bind the
authenticated run, commit identities, Environment approval history and
reviewer. The Testnet job runs the repository lifecycle runner itself against
a dedicated ephemeral PostgreSQL 17 service, passes only Testnet credentials,
then binds the runner-created artifact and Testnet Environment approval
history. The producer does not accept an arbitrary trial path or caller-supplied
PASS JSON.

These signed subjects prove source-bound CI outcomes, reviewer authorization,
or one completed Testnet execution as recorded by the runner. They do not
prove Mainnet behavior, strategy profitability, a campaign approval, or
permission to ARM. The workflows have not run against GitHub in this branch;
no real signed Track C subject or verifier certificate has yet been produced,
so the certificate shape and external REST responses remain unverified.

Protected Testnet lifecycle evidence additionally requires a fresh, signed
read-back of the owned STOP_MARKET and TAKE_PROFIT_MARKET Algo orders in the
REST client's final pre-mutation callback, after throttling and lease fencing
and immediately before the single market-close request. The artifact binds
the read-back time to the submission barrier within five seconds, and records
the close as `MARKET` with `reduceOnly=true`, verified by exchange read-back.
The Testnet protection Algo orders now use the exact same fill-sized, reduce-only
shape as Local Mainnet (`closePosition=false`, `reduceOnly=true`, with `quantity`
matching the entry fill), implemented via `_submit_testnet_protection_algo`
delegating directly to `_submit_local_mainnet_protection_algo`. Both the Python
Track C verifier (`valid_algo` in `scripts/local_pilot_track_c.py`) and TypeScript
readiness verifier (`src/backend/local-live-pilot-readiness.ts`) strictly enforce
this shape, requiring non-zero decimal quantities, `close_position: false`,
`reduce_only: true`, and quantity parity across stop and target legs.

**The Testnet and Local Mainnet paths now share identical protection bracket semantics.**
Both submit each stop/target Algo with `reduceOnly=true`, `closePosition=false` and an explicit
fill-sized `quantity` for `positionSide=BOTH`. The final emergency/market close is `reduceOnly=true`
on both paths. Following WP5 (`OPS-03`), the Track C verification (`_testnet_close_protection_is_proven`),
the emergency close barrier (`_close_owned_testnet_trial_locked`), and the Worker lifecycle
(`close_protected_ethusdc_testnet_trial`) validate this unified bracket shape.

Human prerequisites are to configure main branch protection, pilot-review
required user reviewers/prevent_self_review/main-only branch policy, and the
protected testnet Environment. Under the SOLO_OPERATOR review policy the
operator approves each of the three review dispatches through "Review
deployments" (never admin bypass), and an authorized Testnet lifecycle
dispatch must generate its real bundle. No such dispatch has run yet; the
workflow can only be dispatched after it exists on `main`.

Focused tests exercise shared complete TS/Python phase outputs, signed-class
readiness wiring with an explicitly mocked cryptographic verifier, malformed
payloads, missing/forged local evidence, wrong binding, dirty-source/change
rejection, class/run/event/SHA replay, freshness, verifier failure and reviewer
independence. These mocks verify gate logic only. No real signed bundle has
confirmed the certificate JSON shape yet, so unsupported output fails closed.
Full-suite/CI/real-bundle acceptance remains NOT_RUN for this slice.

## Binding parity and the Worker image (Phase 0)

The Track C binding (`gitSha`, `sourceSha256`, `dependencySha256`,
`migrationSha256`, `pilotPolicySha256`) is computed three times and all three
must agree: `scripts/local_pilot_track_c_source.py` (authority; used by the
verifier), `src/backend/local-release-runtime.ts` (`computeTrackCBinding`;
`computeLocalReleaseFingerprint`, whose fields feed the `--binding`, derives
from it) and `apps/trading_worker/venues/binance/local_pilot_readiness.py`
(which now imports the Python helper instead of keeping its own copy). All use
`git ls-files` tracked files only, the same path lists (including
`scripts/run_local_pilot_ci_regressions.py` and `requirements-worker.lock`),
code-point ordering, symlink rejection and the same byte hashing.
`tests/local-pilot-binding-parity.test.ts` and
`tests/python/test_local_pilot_binding_parity.py` run the real Python helpers
(no mocks) and fail if any list or hash drifts. The persisted per-strategy
hash (`localLivePilotStrategySha256`) deliberately keeps its original
four-file dependency list.

Hashes are over working-tree bytes, so a CRLF working copy of an LF blob
changes the binding. `scripts/check_tracked_eol.py`, `tests/python/test_tracked_eol.py`
and a CI step over `git ls-files --eol` fail on CRLF/mixed endings. (The extra
CI step does not break CHECKS evidence: `validate_payload` requires each name in
`CHECK_STEPS` to appear exactly once and succeed, and tolerates other steps.)

"Clean tree" is judged by the Python helper with Git's user/global config
scrubbed. A file hidden only by a *global* ignore file (for example an
untracked `.claude/settings.local.json`) is invisible to a plain `git status`
and to the TypeScript `committedClean`, but makes `source_binding` raise
`LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN`. The repository `.gitignore` has no
`.claude/` entry; adding one would change what the gate calls dirty and is left
as a human decision. The disagreement is fail-closed (TS clean, Python dirty).

### Containerized Worker evidence path: HMAC-signed readiness verdict

In Docker, the containerized Worker does not mount the host `.git`, `gh` binary,
or `artifacts/` tree. Instead, the TypeScript Control Plane executes the Track C
verifier on the host checkout, and issues a short-lived `PilotReadinessVerdict`
(Decision D3) to the Worker.

1. **Host Verification:** Control Plane verifies all 5 Track C attestations against
   the host checkout and active binding using `scripts/verify_local_pilot_track_c.py`.
2. **Deterministic Signing:** The Control Plane constructs a `PilotReadinessVerdict`
   payload containing `campaignId`, `gitSha`, the 5 Track C hashes, the verified classes,
   and an expiration timestamp (`expiresAt` bounded by `min(now + 300s, oldest_attestation + 24h, campaign_expiry)`,
   with hard maximum TTL cap of 3600s). It signs the canonical JSON representation using
   HMAC-SHA256 with the shared `WORKER_IDENTITY_TOKEN`.
3. **Delivery & Enforcement:** The verdict is delivered during `/arm` and refreshed periodically
   every ~4 minutes by a background timer in `server.ts` via supervisor heartbeats (`/supervisor/heartbeat`
   and `/local-pilot/verdict`). If the campaign is revoked, deactivated, or enters close-only, the timer
   is cleared and null verdict is dispatched to immediately disarm the Worker.
4. **Worker Verification:** In `apps/trading_worker/venues/binance/local_pilot_verdict.py`,
   the Worker verifies the HMAC signature, confirms `campaignId` and `gitSha` match
   its container environment (`LOCAL_LIVE_PILOT_CAMPAIGN_ID` and `LOCAL_LIVE_PILOT_GIT_SHA`),
   and verifies that the binding hashes match its baked-in environment variables.
   If valid and unexpired, the Worker gate reports `READY` (`can_start=True`). If
   expired, missing, or altered, the Worker disarms immediately and fails closed.


### Fetching the signed artifacts

`scripts/download_local_pilot_track_c.py --sha <main HEAD>` copies the five
subject+bundle pairs from the GitHub Actions artifacts of the already-run
workflows into `artifacts/local-pilot-attestations/<CLASS>.json` and
`<CLASS>.bundle.json`. It is a convenience, not an authority: it only runs
`gh run list` and `gh run download` (argv list, `shell=False`, same trusted
Windows `gh.exe` rule as the verifier), refuses a dirty tree or a SHA that is
not HEAD, never overwrites an existing file, applies the verifier's size caps
(subject 64 KiB, bundle 4 MiB), rejects symlinks and unexpected paths, and
installs nothing unless all five pairs validate. For each class it takes the
newest successful run of the right workflow/event on `main` that carries the
class artifact. Nothing downloaded is executed or trusted; run
`scripts/verify_local_pilot_track_c.py` afterwards.

Artifact names and the on-disk layout inside each artifact come from the
`upload-artifact` steps in the workflows. The exact layout `gh run download`
produces is UNVERIFIED (no real run has been downloaded); the downloader
accepts the file at the artifact root, under `local-pilot-attestations/`, or
under a folder named after the artifact, and fails closed otherwise. The
Testnet artifact also contains `testnet-trial-*.json`, which is deliberately
not installed. The downloader has only been exercised against a fake `gh`.
