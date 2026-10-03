"""Generalized attestation verifier mockup (plan items C1-C5).

Trust model: `evidence` (the blob carrying attestations) is untrusted. Every trust anchor (allowlist,
commit author, tree state, pinned run ids, expected hashes) arrives in `trusted_local_state`, supplied
out-of-band by the gate. Reviewer identity is the verifier-authenticated `signer_identity` claim.

Invariants mirrored from scripts/verify_local_pilot_ci_attestation.py:
- fail closed on any exception, size or shape problem
- freshness window -2s..86400s, tz-aware only
- a blocker clears only when its own class verifies
- verifier subprocess receives a minimal, secret-free environment
The real verifier (gh attestation verify) is replaced by an injectable callable.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from base64 import b64decode
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping

REPOSITORY = "kotorn/Blessing-AI"
REPOSITORY_ID = "1366161771"
REF = "refs/heads/main"
MIN_AGE_S = -2
MAX_AGE_S = 86_400
ENV_ALLOWLIST = frozenset({"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"})

B_CHECK = "LOCAL_PILOT_CHECK_PROVENANCE_UNVERIFIED"
B_REVIEW = "LOCAL_PILOT_REVIEW_PROVENANCE_UNVERIFIED"
B_TESTNET = "LOCAL_PILOT_TESTNET_PROVENANCE_UNVERIFIED"
B_RUNTIME = "LOCAL_PILOT_RUNTIME_EVIDENCE_NOT_VERIFIED"
B_DIRTY = "LOCAL_PILOT_WORKING_TREE_DIRTY"


class EvidenceType(str, Enum):
    CHECKS = "CHECKS"
    REVIEW_AUTH_RELEASE = "REVIEW_AUTH_RELEASE"
    REVIEW_ORDER_RISK = "REVIEW_ORDER_RISK"
    REVIEW_PERSISTENCE = "REVIEW_PERSISTENCE"
    TESTNET_ETHUSDC = "TESTNET_ETHUSDC"


REVIEW_TYPES = (EvidenceType.REVIEW_AUTH_RELEASE, EvidenceType.REVIEW_ORDER_RISK,
                EvidenceType.REVIEW_PERSISTENCE)

# Each class is signed by its own workflow so a CI cert cannot stand in for a review.
WORKFLOW_FOR = {
    EvidenceType.CHECKS: f"{REPOSITORY}/.github/workflows/ci.yml@{REF}",
    EvidenceType.REVIEW_AUTH_RELEASE: f"{REPOSITORY}/.github/workflows/attest-review.yml@{REF}",
    EvidenceType.REVIEW_ORDER_RISK: f"{REPOSITORY}/.github/workflows/attest-review.yml@{REF}",
    EvidenceType.REVIEW_PERSISTENCE: f"{REPOSITORY}/.github/workflows/attest-review.yml@{REF}",
    EvidenceType.TESTNET_ETHUSDC: f"{REPOSITORY}/.github/workflows/attest-testnet.yml@{REF}",
}

SUBJECT_FIELDS = ("evidence_type", "git_sha", "source_sha256", "dependency_sha256",
                  "migration_sha256", "policy_sha256", "subject_sha256", "repository_id",
                  "workflow_ref", "run_id", "observed_at")
HASH_FIELDS = ("source_sha256", "dependency_sha256", "migration_sha256", "policy_sha256")

# verifier(statement_bytes, bundle, expected, env) -> signed claims dict; raises on failure.
Verifier = Callable[[bytes, str, Mapping[str, str], Mapping[str, str]], Mapping[str, Any]]


class _Reject(Exception):
    pass


class VerificationFailed(Exception):
    """Raised by a verifier for a definitive negative verdict (maps to nonzero gh exit)."""


def canonical_statement(subject: Mapping[str, Any]) -> bytes:
    return json.dumps(subject, sort_keys=True, separators=(",", ":")).encode()


def minimal_env(environ: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in environ.items() if k.upper() in ENV_ALLOWLIST}


def _is_hex(value: Any, n: int) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{n}}}", value) is not None


def normalize_identity(value: Any) -> str:
    """Canonical form for every identity comparison: NFKC, strip, casefold.

    Raises ValueError for non-strings, empty results, or any control/format/unassigned character
    (zero-width characters would otherwise let two spellings of one person compare unequal).
    """
    if not isinstance(value, str):
        raise ValueError("identity must be a string")
    text = unicodedata.normalize("NFKC", value).strip().casefold()
    if not text or any(unicodedata.category(ch)[0] == "C" for ch in text):
        raise ValueError("identity empty or contains control/format characters")
    return text


_LOGIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?(?:\[bot\])?", re.ASCII)
_LINE_SEPARATORS = ("\u2028", "\u2029")


def normalize_login(value: Any) -> str:
    """Canonical GitHub login: NFKC, strip, casefold, then must fully match the ASCII login grammar.

    Grammar: [a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])? with an optional literal "[bot]" suffix. Combining marks,
    homoglyphs, underscores, spaces, U+2028/U+2029 (anywhere, even at the edges) and anything non-ASCII
    left after NFKC are rejected with ValueError.
    """
    if isinstance(value, str) and any(sep in value for sep in _LINE_SEPARATORS):
        raise ValueError("line separator in identity")
    text = normalize_identity(value)
    if _LOGIN.fullmatch(text) is None:
        raise ValueError("identity is not a GitHub login")
    return text


def _login_set(value: Any, name: str, *, non_empty: bool) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):  # a bare str would iterate by char
        raise ValueError(f"{name} type")
    logins = frozenset(normalize_login(item) for item in value)
    if non_empty and not logins:
        raise ValueError(f"{name} empty")
    return logins


_RFC3339 = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})", re.ASCII)


def parse_rfc3339(value: Any) -> datetime:
    """Strict RFC 3339 date-time: uppercase T/Z, ASCII digits, explicit offset, seconds 00-59.

    Fractions of 1-9 digits are accepted and truncated to microseconds. Raises ValueError otherwise.
    """
    match = _RFC3339.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("not a strict RFC 3339 timestamp")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    micro = int((match.group(7) or "").ljust(6, "0")[:6])
    zone = match.group(8)
    if zone == "Z":
        tz = UTC
    else:
        off_h, off_m = int(zone[1:3]), int(zone[4:6])
        if off_h > 23 or off_m > 59:
            raise ValueError("bad offset")
        delta = timedelta(hours=off_h, minutes=off_m)
        tz = timezone(delta if zone[0] == "+" else -delta)
    return datetime(year, month, day, hour, minute, second, micro, tzinfo=tz)  # ValueError if invalid


def _parse_time(value: Any) -> datetime:
    try:
        return parse_rfc3339(value)
    except ValueError as error:
        raise _Reject("observed_at") from error


def _load_trusted(state: Any) -> dict[str, Any]:
    """Validate the out-of-band trust anchors. Any defect raises (global fail-closed)."""
    if not isinstance(state, Mapping):
        raise ValueError("trusted_local_state")
    if not _is_hex(state.get("git_sha"), 40) or any(not _is_hex(state.get(f), 64) for f in HASH_FIELDS):
        raise ValueError("trusted hashes")
    allowlist = _login_set(state.get("reviewer_allowlist"), "reviewer_allowlist", non_empty=False)
    authors = _login_set(state.get("commit_authors"), "commit_authors", non_empty=True)
    pinned = state.get("pinned_run_ids")
    if not isinstance(pinned, Mapping):
        raise ValueError("pinned_run_ids")
    return {"git_sha": state["git_sha"], **{f: state[f] for f in HASH_FIELDS},
            "commit_authors": authors, "allowlist": allowlist,
            "pinned_run_ids": dict(pinned), "tree_dirty": state.get("tree_dirty")}


def _verify_one(etype: EvidenceType, entry: Any, trusted: Mapping[str, Any], verifier: Verifier,
                env: Mapping[str, str], now: datetime) -> str:
    """Return the normalized authenticated signer identity, or raise _Reject. Verifier crashes propagate."""
    if not isinstance(entry, Mapping) or not isinstance(entry.get("subject"), Mapping):
        raise _Reject("shape")
    subject = copy.deepcopy(dict(entry["subject"]))  # frozen: later edits to the live evidence change nothing
    if set(subject) != set(SUBJECT_FIELDS):
        raise _Reject("fields")
    if subject["evidence_type"] != etype.value:  # cross-class replay
        raise _Reject("evidence_type")
    if not _is_hex(subject["git_sha"], 40) or subject["git_sha"] != trusted["git_sha"]:
        raise _Reject("git_sha")
    for name in HASH_FIELDS:
        if not _is_hex(subject[name], 64) or subject[name] != trusted[name]:
            raise _Reject(name)
    if subject["repository_id"] != REPOSITORY_ID:
        raise _Reject("repository_id")
    if subject["workflow_ref"] != WORKFLOW_FOR[etype]:
        raise _Reject("workflow_ref")
    pinned = trusted["pinned_run_ids"].get(etype.value)
    if not isinstance(pinned, str) or not pinned or subject["run_id"] != pinned:
        raise _Reject("run_id")
    age = (now - _parse_time(subject["observed_at"])).total_seconds()
    if not MIN_AGE_S <= age <= MAX_AGE_S:
        raise _Reject("freshness")
    encoded = entry.get("artifact_b64", "")  # read before the verifier runs
    try:
        artifact = b64decode(encoded, validate=True) if isinstance(encoded, str) else None
    except ValueError:  # binascii.Error subclasses ValueError
        artifact = None
    if artifact is None or hashlib.sha256(artifact).hexdigest() != subject["subject_sha256"]:
        raise _Reject("subject_sha256")  # swapped, malformed or missing bytes
    bundle = entry.get("bundle")
    if not isinstance(bundle, str) or not bundle:
        raise _Reject("bundle")
    statement = canonical_statement(subject)
    # The verifier contract: signed claims must bind identity to exactly this subject (see README).
    expected = {"repository": REPOSITORY, "repository_id": REPOSITORY_ID,
                "workflow_ref": WORKFLOW_FOR[etype], "git_sha": subject["git_sha"],
                "evidence_type": etype.value, "subject_sha256": subject["subject_sha256"],
                "run_id": subject["run_id"]}
    try:  # the verifier gets copies; the gate keeps the originals for the post-call comparison
        claims = verifier(statement, bundle, copy.deepcopy(expected), dict(env))
    except VerificationFailed as error:  # definitive "signature/cert does not match"
        raise _Reject("verification") from error
    except Exception as error:  # crash, timeout, OSError: not a verdict, fail closed globally
        raise _VerifierFailure from error
    # Defence in depth: signed claims must equal every field we relied on and every field we asked for.
    if not isinstance(claims, Mapping):
        raise _Reject("claims")
    if any(claims.get(f) != subject[f] for f in SUBJECT_FIELDS) \
            or any(claims.get(k) != v for k, v in expected.items()):
        raise _Reject("claims")
    try:
        return normalize_login(claims.get("signer_identity"))
    except ValueError as error:
        raise _Reject("identity") from error


class _VerifierFailure(Exception):
    """Verifier crashed/timed out/refused: whole evaluation fails closed."""


def evaluate(evidence: Mapping[str, Any], verifier: Verifier, trusted_local_state: Mapping[str, Any], *,
             now: datetime | None = None, environ: Mapping[str, str] | None = None) -> dict:
    """Return {"blockers": [...], "can_approve": bool}. Never raises.

    `evidence` is untrusted and is read only for its "attestations" mapping. `trusted_local_state` is
    supplied out-of-band by the gate and holds git_sha, the four *_sha256 values, commit_authors,
    reviewer_allowlist, tree_dirty and pinned_run_ids. Nothing else in `evidence` is ever consulted.
    """
    try:
        return _evaluate(evidence, verifier, trusted_local_state, now or datetime.now(UTC), environ or {})
    except Exception:  # fail closed: verifier crash, timeout, malformed evidence or trusted state
        return {"blockers": sorted([B_RUNTIME, B_CHECK, B_REVIEW, B_TESTNET]), "can_approve": False}


def _evaluate(evidence, verifier, trusted_local_state, now, environ) -> dict:
    trusted = _load_trusted(trusted_local_state)
    if not isinstance(evidence, Mapping):
        raise ValueError("evidence")
    attestations = evidence.get("attestations")
    if attestations is None:
        attestations = {}
    if not isinstance(attestations, Mapping):
        raise ValueError("attestations")
    env = minimal_env(environ)
    ok: dict[EvidenceType, str] = {}
    for etype in EvidenceType:
        if etype.value not in attestations:
            continue
        try:
            ok[etype] = _verify_one(etype, attestations[etype.value], trusted, verifier, env, now)
        except _Reject:
            continue
    blockers: set[str] = set()
    if EvidenceType.CHECKS not in ok:
        blockers.add(B_CHECK)
    if EvidenceType.TESTNET_ETHUSDC not in ok:
        blockers.add(B_TESTNET)
    reviewers = [ok.get(t) for t in REVIEW_TYPES]
    names = [r for r in reviewers if r]
    if (None in reviewers or len(set(names)) != 3 or not trusted["commit_authors"].isdisjoint(names)
            or not all(n in trusted["allowlist"] for n in names)):
        blockers.add(B_REVIEW)
    if trusted["tree_dirty"] is not False:
        blockers.add(B_DIRTY)
    return {"blockers": sorted(blockers), "can_approve": not blockers}
