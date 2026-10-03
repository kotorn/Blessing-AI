# Attestation channel (design mockup, C1-C5)

Status: MOCKUP. Fake fixtures only. Not imported by production code, does not change readiness status,
touches no gate, makes no network or `gh` calls, uses no secrets. It mirrors the invariants of
`scripts/verify_local_pilot_ci_attestation.py` (fail closed, freshness -2s..86400s, tz-aware only, minimal
verifier env, per-class clearing) and tightens two things the real script does lazily: timestamps are strict
RFC 3339 here (the real script uses `fromisoformat(...replace("Z", "+00:00"))`), and identities are normalized.

## Model
- `evidence_type`: CHECKS, REVIEW_AUTH_RELEASE, REVIEW_ORDER_RISK, REVIEW_PERSISTENCE, TESTNET_ETHUSDC.
- Each subject binds evidence_type, 40-hex git sha, source/dependency/migration/policy sha256,
  subject_sha256 (hash of the exact artifact bytes), repository id 1366161771, per-class workflow ref,
  pinned run_id, and signed `observed_at` (age window -2s..86400s inclusive).
- `evaluate(evidence, verifier, trusted_local_state, *, now=, environ=)` returns
  `{"blockers": [...], "can_approve": bool}` and never raises. Blockers: CHECK / REVIEW / TESTNET
  `..._PROVENANCE_UNVERIFIED` clear only when their own class verifies; `LOCAL_PILOT_WORKING_TREE_DIRTY` unless
  the trusted tree state is exactly `false`; `LOCAL_PILOT_RUNTIME_EVIDENCE_NOT_VERIFIED` (plus all three class
  blockers) when the verifier raises/times out (even after earlier classes verified), or when the evidence or
  the trusted state is malformed. That fail-closed set is exactly those four blockers; it does not add
  `LOCAL_PILOT_WORKING_TREE_DIRTY` (the tree state is unknown or untrusted there, and `can_approve` is false regardless).
- `can_approve` is `true` iff `blockers` is empty. On the success path (evaluation completed) the
  `RUNTIME_EVIDENCE_NOT_VERIFIED` blocker is never emitted, so `can_approve` does not depend on it; it appears
  only on the fail-closed path, where `can_approve` is false anyway. A consumer must not treat its absence as
  proof that runtime evidence exists.

## Trust-anchor rule
`evidence` is the blob that carries the attestations. It is untrusted and is read for exactly one thing: its
`attestations` mapping. These values are trust anchors and MUST come from `trusted_local_state`, which the gate
builds out-of-band from the local checkout, the pinned CI run records and a reviewer config it controls:

| key | meaning |
| --- | --- |
| `git_sha`, `source_sha256`, `dependency_sha256`, `migration_sha256`, `policy_sha256` | what the gate computed locally |
| `commit_authors` | non-empty list/set/tuple/frozenset of GitHub logins covering every author, committer and co-author (`Co-authored-by`) in the reviewed commit range. See "Identity format". The old single `commit_author` key is not read; a state without `commit_authors` fails closed |
| `reviewer_allowlist` | list/set/tuple/frozenset of GitHub logins (may be empty, which blocks every reviewer). A bare string, dict, None or any element that is not a valid login makes the whole evaluation fail closed |
| `tree_dirty` | must be exactly `false` |
| `pinned_run_ids` | `{evidence_type: run_id}`, a missing entry blocks that class |

`local`, `reviewer_allowlist`, `commit_authors`, `commit_author`, `tree_dirty`, `pinned_run_ids` keys inside `evidence` are ignored
(tests prove it). `trusted_local_state` is a required positional argument.

## Identity format
Every identity (signed `signer_identity`, each `commit_authors` entry, each allowlist entry) is a GitHub login,
not a git `user.name` and not an email: git name "Kan Chang" or `kan@example.com` is rejected, the login
`kotorn` is what belongs here. The gate must map commit authors, committers and `Co-authored-by` trailers to
logins itself (for example via the GitHub API) before building `trusted_local_state`; anything it cannot map
must never be silently dropped: if any author cannot be mapped, the gate must fail closed instead of building the list.

`normalize_login` applies `normalize_identity` (Unicode NFKC, strip, casefold; empty results and any
control/format/unassigned character are rejected) and then requires a full match of
`[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?` with an optional literal `[bot]` suffix, ASCII only (`re.ASCII`).
U+2028/U+2029 are rejected anywhere, even at the edges. So combining marks (`kotorn` + U+0301), homoglyphs
(Cyrillic o), underscores, spaces, `@`, over-long logins and zero-width characters are all invalid.
Consequences: an invalid signer identity blocks only that class (the review class for a review slot);
an invalid allowlist entry or `commit_authors` entry fails the whole evaluation closed. Trailing whitespace,
NBSP, case and full-width spellings still normalize to the plain login, so they cannot dodge the author check.
A TS port must reproduce this exactly (`normalize("NFKC")`, Unicode whitespace trim, full casefold, then the regex).

## Verifier isolation
The verifier receives a deep copy of `expected` and a canonical statement built from a deep snapshot of the
subject taken before the call. The gate compares the returned claims with its own untouched copies, so a
verifier that mutates `expected` (or the live evidence) cannot make forged claims match.

## Verifier contract (what a real GitHub Environment-approval attestation must carry)
`verifier(statement_bytes, bundle, expected, env) -> claims`, where `statement_bytes` is the canonical JSON of
the subject (sorted keys, no spaces) and `expected` is
`{repository, repository_id, workflow_ref, git_sha, evidence_type, subject_sha256, run_id}`.

The verifier must (1) verify the signature and certificate chain over exactly `statement_bytes`, (2) check the
signing workflow/ref/repo/repo id/commit against `expected`, and (3) return the signed claims. Raise
`VerificationFailed` for a definitive negative; any other exception, timeout or OSError closes the whole gate.

The returned claims must be a mapping that:
- equals the subject on every subject field and equals every `expected` entry (so claims for another subject,
  another class, another run or another artifact hash block that class; missing keys block it);
- includes `signer_identity`: a non-empty string naming the authenticated human who approved this exact
  subject, e.g. the GitHub login recorded for the Environment deployment approval of that run. It must be
  bound into the signed material (certificate extension or signed predicate), not copied from the evidence
  blob, and it must not be the workflow identity: all three review classes share one workflow, so the workflow
  cannot distinguish reviewers. An approval attestation therefore needs: run_id, subject_sha256, evidence_type,
  repository/repository id, commit sha, workflow ref, and the approver login, all inside the signature.

Review rule: the three `signer_identity` values, after normalization, must be pairwise distinct, none equal to
ANY normalized entry of `commit_authors`, and all in the normalized allowlist. Free-text `reviewer` fields are ignored.

## Parity fixture
`fixtures/parity_cases.json` is generated by `python -m attestation_channel.fakes` and checked for drift by a
test. Its `base` is `{"evidence": {...}, "trusted": {...}}`. A TS port loads `base`, applies each case in this
order (`apply_case` in `fakes.py`): sign base log; deep-copy; `drop`; `mutations` (dotted-path set, paths start
with `evidence.` or `trusted.`); `alias_bundle`; if `resign`, re-sign the mutated doc (with `signer_override`
keyed by evidence type). Then call `evaluate(doc.evidence, verifier, doc.trusted)` and assert
`expect_blockers` (sorted) and `expect_can_approve`. Case flags: `verifier_raises` with optional
`verifier_raises_after` (N calls succeed first), `environ`, `now`. The fake log maps bundle id to
`{statement_sha256, claims}`; claims = subject + `repository` + `signer_identity`.
Timestamp cases (`time_accepts_*`, `time_rejects_*`) pin the grammar
`YYYY-MM-DDTHH:MM:SS[.f{1,9}](Z|+HH:MM|-HH:MM)`: uppercase T and Z, ASCII digits, seconds 00-59, offset
hours <= 23, minutes <= 59, whole string matched (no trailing whitespace); fractions truncate to microseconds.

## Run
`python -m pytest prototypes/attestation_channel/tests -q`
