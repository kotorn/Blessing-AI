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
The *Testnet* protection Algo orders use `closePosition=true` (no quantity),
which is incompatible with `reduceOnly`; therefore those Testnet protection
records correctly report `reduceOnly=false`
(`_submit_testnet_protection_algo`, `execution.py`; enforced by
`valid_algo` in `scripts/local_pilot_track_c.py`). This documents the
exchange-supported order semantics, not a completed Testnet run. Until an
authorized protected Environment dispatch produces a real signed artifact,
Testnet evidence stays NOT_RUN.

**The Local Mainnet path is different, and the Testnet artifact does not
exercise it.** `_submit_local_mainnet_protection_algo`
(`execution.py`, params block around lines 3594-3595 at `52015b2`) submits each stop/target Algo
with `reduceOnly=true`, `closePosition=false` and an explicit fill-sized
`quantity`, only for `positionSide=BOTH`. The shared read-back in
`protection.py` accepts either shape (close-all with no `reduceOnly`, or
`reduceOnly=true` with `quantity` equal to the absolute position). The final
emergency/market close is `reduceOnly=true` on both paths. So the two
statements are each true for their own code path: Testnet protections are
`closePosition=true`/`reduceOnly=false`; Local Mainnet protections are
`reduceOnly=true`/`closePosition=false`. Whether Binance accepts a
reduce-only market close while reduce-only fill-sized Algo orders are open on
Mainnet has not been shown by any Testnet artifact in this repository
(UNVERIFIED).

Human prerequisites are to configure main branch protection, pilot-review
required user reviewers/prevent_self_review/main-only branch policy, and the
protected testnet Environment. Three independent non-author users must
approve separate review dispatches, and an authorized Testnet lifecycle
dispatch must generate its real bundle. No such dispatch or configuration
change occurred in this worktree. The prior environment/protection snapshot
reported missing protections; it was not revalidated in this implementation
turn and must not be treated as current evidence.

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

### Known limitation: the Python gate cannot reach READY inside Docker

`Dockerfile.worker` now copies every `scripts/` module the Worker can import
(`tests/python/test_dockerfile_worker_imports.py` enforces this statically), so
the image is importable. It still cannot satisfy the gate, by static reading
of the image contents (not exercised in a container build, so runtime
behavior is UNVERIFIED):

- there is no `git` in the final image, so `git rev-parse` / `git status`
  return nothing and the clean-commit check reports
  `LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN`;
- there is no `gh` CLI, so `gh attestation verify` cannot run;
- `artifacts/local-pilot-attestations/` and the hashed source files
  (`server.ts`, `src/backend`, workflows, lock files) are not in the image.

The Worker's own Track C result is therefore always BLOCKED in Docker. The
TypeScript control plane verifies on the host checkout. How the containerized
Worker should consume host-verified evidence is an open design decision that
needs a human; it was deliberately not changed.

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
