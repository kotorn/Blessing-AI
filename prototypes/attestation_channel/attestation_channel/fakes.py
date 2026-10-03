"""Fake fixtures and a fake signing log. No real keys, no network, no gh."""

from __future__ import annotations

import copy
import hashlib
import json
from base64 import b64encode
from pathlib import Path
from typing import Any

from .core import (REPOSITORY, REPOSITORY_ID, WORKFLOW_FOR, EvidenceType, VerificationFailed,
                   canonical_statement, evaluate)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "parity_cases.json"
NOW = "2026-10-02T12:00:00+00:00"
GIT_SHA = "a" * 40
SIGNERS = {"REVIEW_AUTH_RELEASE": "fake-reviewer-alice", "REVIEW_ORDER_RISK": "fake-reviewer-bob",
           "REVIEW_PERSISTENCE": "fake-reviewer-carol"}


def h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def build_valid() -> dict[str, Any]:
    """Return {"evidence": <attestation blob>, "trusted": <out-of-band gate state>}."""
    trusted: dict[str, Any] = {
        "git_sha": GIT_SHA, "source_sha256": h("src"), "dependency_sha256": h("deps"),
        "migration_sha256": h("mig"), "policy_sha256": h("policy"),
        "commit_authors": ["fake-author-dave"], "tree_dirty": False, "pinned_run_ids": {},
        "reviewer_allowlist": sorted(SIGNERS.values()) + ["fake-reviewer-erin"]}
    attestations: dict[str, Any] = {}
    for index, etype in enumerate(EvidenceType):
        artifact = f"fake artifact bytes for {etype.value}".encode()
        run_id = str(9000 + index)
        trusted["pinned_run_ids"][etype.value] = run_id
        subject = {
            "evidence_type": etype.value, "git_sha": GIT_SHA,
            "source_sha256": trusted["source_sha256"], "dependency_sha256": trusted["dependency_sha256"],
            "migration_sha256": trusted["migration_sha256"], "policy_sha256": trusted["policy_sha256"],
            "subject_sha256": hashlib.sha256(artifact).hexdigest(),
            "repository_id": REPOSITORY_ID, "workflow_ref": WORKFLOW_FOR[etype],
            "run_id": run_id, "observed_at": "2026-10-02T11:00:00+00:00"}
        attestations[etype.value] = {"subject": subject, "bundle": f"fake-bundle-{etype.value}",
                                     "artifact_b64": b64encode(artifact).decode()}
    return {"evidence": {"attestations": attestations}, "trusted": trusted}


def evaluate_doc(doc: dict[str, Any], verifier, **kwargs: Any) -> dict:
    """Run core.evaluate with evidence and trusted state kept strictly separate."""
    return evaluate(doc["evidence"], verifier, doc["trusted"], **kwargs)


def sign_log(doc: dict[str, Any], signer_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fake transparency log: bundle id -> {statement_sha256, claims}. Claims mimic the cert.

    Claims carry the full subject plus `repository` and `signer_identity` (the authenticated human
    or workflow identity that signed this exact subject).
    """
    signers = {**SIGNERS, **(signer_override or {})}
    log: dict[str, Any] = {}
    for key, entry in doc["evidence"]["attestations"].items():
        subject = entry["subject"]
        claims = dict(subject)
        claims["repository"] = REPOSITORY
        claims["signer_identity"] = signers.get(key, "fake-ci-bot")
        log[entry["bundle"]] = {
            "statement_sha256": hashlib.sha256(canonical_statement(subject)).hexdigest(),
            "claims": claims}
    return log


def fake_verifier(log: dict[str, Any], calls: list | None = None, raises: Exception | None = None,
                  raises_after: int = 0):
    """raises_after=N lets the first N calls succeed normally, then raises (partial failure)."""
    seen = [0]

    def verify(statement: bytes, bundle: str, expected, env):
        if calls is not None:
            calls.append({"bundle": bundle, "expected": dict(expected), "env": dict(env)})
        seen[0] += 1
        if raises is not None and seen[0] > raises_after:
            raise raises
        record = log.get(bundle)
        if record is None or record["statement_sha256"] != hashlib.sha256(statement).hexdigest():
            raise VerificationFailed("fake verification failed")
        return record["claims"]
    return verify


def set_path(doc: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        doc = doc[part]
    doc[parts[-1]] = value


def apply_case(case: dict[str, Any], base: dict[str, Any]) -> tuple[dict, dict]:
    """Apply a declarative case. Returns (doc, signing_log).

    A doc is {"evidence": ..., "trusted": ...}; mutation paths start with "evidence." or "trusted.".
    Order (a TS port must follow it): log = sign(base); deep-copy; drop; mutations; alias_bundle;
    if resign, log = sign(mutated doc, signer_override).
    """
    log = sign_log(base)
    doc = copy.deepcopy(base)
    for key in case.get("drop", []):
        doc["evidence"]["attestations"].pop(key, None)
    for mutation in case.get("mutations", []):
        set_path(doc, mutation["path"], mutation["set"])
    for alias in case.get("alias_bundle", []):  # replay: reuse another class's bundle id
        atts = doc["evidence"]["attestations"]
        atts[alias["to"]]["bundle"] = atts[alias["from"]]["bundle"]
    if case.get("resign"):
        log = sign_log(doc, case.get("signer_override"))
    return doc, log


A = "evidence.attestations."
T = "trusted."
CHK = "LOCAL_PILOT_CHECK_PROVENANCE_UNVERIFIED"
REV = "LOCAL_PILOT_REVIEW_PROVENANCE_UNVERIFIED"
TST = "LOCAL_PILOT_TESTNET_PROVENANCE_UNVERIFIED"
RUN = "LOCAL_PILOT_RUNTIME_EVIDENCE_NOT_VERIFIED"
DIRTY = "LOCAL_PILOT_WORKING_TREE_DIRTY"
ALL_TYPES = [e.value for e in EvidenceType]
ALL_FAIL = [CHK, REV, TST, RUN]
OBS = A + "CHECKS.subject.observed_at"


def _m(path: str, value: Any) -> dict:
    return {"path": path, "set": value}


def _case(name: str, expect: list[str], **extra: Any) -> dict:
    return {"name": name, "expect_blockers": sorted(expect), "expect_can_approve": not expect, **extra}


def _resigned(name: str, expect: list[str], path: str, value: Any, **extra: Any) -> dict:
    return _case(name, expect, resign=True, mutations=[_m(path, value)], **extra)


def _time(name: str, expect: list[str], value: Any) -> dict:
    return _resigned(name, expect, OBS, value)


# Strict RFC 3339 date-time accepted by the gate: YYYY-MM-DDTHH:MM:SS[.f{1,9}](Z|+HH:MM|-HH:MM),
# uppercase T and Z, ASCII digits only, seconds 00-59 (no leap second), explicit offset required.
TIME_CASES = [
    _time("time_accepts_z_suffix", [], "2026-10-02T11:00:00Z"),
    _time("time_accepts_fraction_z", [], "2026-10-02T11:00:00.123456Z"),
    _time("time_accepts_nine_digit_fraction", [], "2026-10-02T11:00:00.123456789Z"),
    _time("time_accepts_positive_offset", [], "2026-10-02T18:00:00+07:00"),
    _time("time_accepts_negative_offset", [], "2026-10-02T06:00:00-05:00"),
    _time("time_rejects_compact_form", [CHK], "20261002T110000Z"),
    _time("time_rejects_compact_offset", [CHK], "2026-10-02T11:00:00+0000"),
    _time("time_rejects_hour_only_offset", [CHK], "2026-10-02T11:00:00+00"),
    _time("time_rejects_lowercase_z", [CHK], "2026-10-02T11:00:00z"),
    _time("time_rejects_lowercase_t", [CHK], "2026-10-02t11:00:00Z"),
    _time("time_rejects_space_separator", [CHK], "2026-10-02 11:00:00Z"),
    _time("time_rejects_missing_seconds", [CHK], "2026-10-02T11:00Z"),
    _time("time_rejects_date_only", [CHK], "2026-10-02"),
    _time("time_rejects_empty_fraction", [CHK], "2026-10-02T11:00:00.Z"),
    _time("time_rejects_ten_digit_fraction", [CHK], "2026-10-02T11:00:00.1234567890Z"),
    _time("time_rejects_leap_second", [CHK], "2026-10-02T11:00:60Z"),
    _time("time_rejects_offset_hour_24", [CHK], "2026-10-02T11:00:00+24:00"),
    _time("time_rejects_impossible_date", [CHK], "2026-02-30T11:00:00Z"),
    _time("time_rejects_trailing_space", [CHK], "2026-10-02T11:00:00Z "),
    _time("time_rejects_trailing_newline", [CHK], "2026-10-02T11:00:00Z\n"),
    _time("time_rejects_fullwidth_digits", [CHK], "２０２６-10-02T11:00:00Z"),
    _time("time_rejects_non_string", [CHK], 1790938800),
]

CASES = [
    _case("positive_full_fake_set", []),
    _case("no_attestations_at_all", [CHK, REV, TST], drop=ALL_TYPES),
    _case("ci_only_attestation", [REV, TST], drop=[t for t in ALL_TYPES if t != "CHECKS"]),
    _case("missing_testnet_only", [TST], drop=["TESTNET_ETHUSDC"]),
    _case("missing_one_review", [REV], drop=["REVIEW_ORDER_RISK"]),
    _case("missing_checks_only", [CHK], drop=["CHECKS"]),
    _case("cross_class_replay_checks_bundle_in_testnet_slot", [TST],
          alias_bundle=[{"from": "CHECKS", "to": "TESTNET_ETHUSDC"}]),
    _resigned("cross_class_replay_resigned_checks_type_in_testnet_slot", [TST],
              A + "TESTNET_ETHUSDC.subject.evidence_type", "CHECKS"),
    _resigned("review_type_swapped_resigned", [REV],
              A + "REVIEW_ORDER_RISK.subject.evidence_type", "REVIEW_AUTH_RELEASE"),
    _case("swapped_artifact_bytes", [CHK], mutations=[_m(A + "CHECKS.artifact_b64", "c3dhcHBlZA==")]),
    _case("malformed_artifact_base64_blocks_class_only", [CHK],
          mutations=[_m(A + "CHECKS.artifact_b64", "!!not-base64!!")]),
    _case("swapped_subject_sha_without_resign", [CHK],
          mutations=[_m(A + "CHECKS.subject.subject_sha256", "b" * 64)]),
    _resigned("swapped_subject_sha_resigned", [CHK], A + "CHECKS.subject.subject_sha256", "b" * 64),
    _resigned("swapped_git_sha_resigned", [TST], A + "TESTNET_ETHUSDC.subject.git_sha", "c" * 40),
    _resigned("swapped_source_sha_resigned", [CHK], A + "CHECKS.subject.source_sha256", "d" * 64),
    _resigned("swapped_repo_id_resigned", [CHK], A + "CHECKS.subject.repository_id", "1"),
    _resigned("swapped_workflow_ref_resigned", [CHK], A + "CHECKS.subject.workflow_ref",
              "evil/fork/.github/workflows/ci.yml@refs/heads/main"),
    _resigned("swapped_run_id_resigned", [TST], A + "TESTNET_ETHUSDC.subject.run_id", "1"),
    _resigned("swapped_evidence_type_resigned", [CHK], A + "CHECKS.subject.evidence_type", "TESTNET_ETHUSDC"),
    _resigned("fresh_at_86400s", [], OBS, "2026-10-01T12:00:00+00:00"),
    _resigned("stale_at_86401s", [CHK], OBS, "2026-10-01T11:59:59+00:00"),
    _resigned("future_plus_2s_allowed", [], OBS, "2026-10-02T12:00:02+00:00"),
    _resigned("future_plus_3s_rejected", [CHK], OBS, "2026-10-02T12:00:03+00:00"),
    _resigned("naive_timestamp_rejected", [TST], A + "TESTNET_ETHUSDC.subject.observed_at",
              "2026-10-02T11:00:00"),
    *TIME_CASES,
    _case("dirty_tree", [DIRTY], mutations=[_m(T + "tree_dirty", True)]),
    _case("dirty_tree_non_boolean_value", [DIRTY], mutations=[_m(T + "tree_dirty", "false")]),
    # --- reviewer identity: signed, normalized, distinct, allowlisted -------------------------
    _case("duplicate_reviewer_identity", [REV], resign=True,
          signer_override={"REVIEW_PERSISTENCE": "fake-reviewer-alice"}),
    _case("duplicate_reviewer_identity_case_and_whitespace", [REV], resign=True,
          signer_override={"REVIEW_PERSISTENCE": "  Fake-Reviewer-ALICE \t"}),
    _case("duplicate_reviewer_identity_fullwidth_nfkc", [REV], resign=True,
          signer_override={"REVIEW_PERSISTENCE": "ｆａｋｅ-reviewer-alice"}),
    _case("reviewer_is_commit_author_case_insensitive", [REV],
          mutations=[_m(T + "commit_authors", ["Fake-Reviewer-Bob"])]),
    _case("commit_author_trailing_space_does_not_bypass", [REV],
          mutations=[_m(T + "commit_authors", ["fake-reviewer-bob "])]),
    _case("commit_author_leading_newline_does_not_bypass", [REV],
          mutations=[_m(T + "commit_authors", ["\nfake-reviewer-bob"])]),
    _case("any_listed_commit_author_blocks_reviewer", [REV],
          mutations=[_m(T + "commit_authors", ["fake-author-dave", "fake-author-zed", "fake-reviewer-carol"])]),
    _case("co_author_bot_and_unrelated_authors_do_not_block", [],
          mutations=[_m(T + "commit_authors", ["fake-author-dave", "dependabot[bot]", "fake-author-zed"])]),
    _case("signed_reviewer_trailing_space_equals_author", [REV], resign=True,
          mutations=[_m(T + "commit_authors", ["fake-reviewer-bob"])],
          signer_override={"REVIEW_ORDER_RISK": "fake-reviewer-bob "}),
    _case("signed_reviewer_nbsp_equals_author", [REV], resign=True,
          mutations=[_m(T + "commit_authors", ["fake-reviewer-bob"])],
          signer_override={"REVIEW_ORDER_RISK": "fake-reviewer-bob "}),
    _case("signed_reviewer_zero_width_char_rejected", [REV], resign=True,
          signer_override={"REVIEW_ORDER_RISK": "fake-reviewer-bob​"}),
    _case("allowlist_entries_are_normalized", [],
          mutations=[_m(T + "reviewer_allowlist",
                        ["  FAKE-reviewer-alice ", "fake-reviewer-bob\t", "Fake-Reviewer-Carol"])]),
    _case("reviewer_not_allowlisted", [REV],
          mutations=[_m(T + "reviewer_allowlist", ["fake-reviewer-alice", "fake-reviewer-bob"])]),
    _case("empty_signer_identity", [REV], resign=True, signer_override={"REVIEW_ORDER_RISK": ""}),
    _case("whitespace_only_signer_identity", [REV], resign=True,
          signer_override={"REVIEW_ORDER_RISK": " \t "}),
    _case("non_string_signer_identity_int", [REV], resign=True,
          signer_override={"REVIEW_ORDER_RISK": 7}),
    _case("non_string_signer_identity_null", [REV], resign=True,
          signer_override={"REVIEW_ORDER_RISK": None}),
    _case("non_string_signer_identity_list", [REV], resign=True,
          signer_override={"REVIEW_ORDER_RISK": ["fake-reviewer-bob"]}),
    _case("empty_signer_identity_on_checks_blocks_checks", [CHK], resign=True,
          signer_override={"CHECKS": ""}),
    _case("free_string_reviewer_in_evidence_is_ignored", [],
          mutations=[_m(A + "REVIEW_ORDER_RISK.reviewer", "fake-author-dave")]),
    # --- trust anchors: values smuggled in the evidence blob are ignored ----------------------
    _case("smuggled_allowlist_in_evidence_ignored", [REV],
          mutations=[_m(T + "reviewer_allowlist", ["fake-reviewer-alice", "fake-reviewer-bob"]),
                     _m("evidence.reviewer_allowlist", ["fake-reviewer-carol", "fake-reviewer-bob"])]),
    _case("smuggled_commit_author_in_evidence_ignored", [REV],
          mutations=[_m(T + "commit_authors", ["fake-reviewer-bob"]),
                     _m("evidence.local", {"commit_author": "someone-else"}),
                     _m("evidence.commit_author", "someone-else")]),
    _case("smuggled_clean_tree_in_evidence_ignored", [DIRTY],
          mutations=[_m(T + "tree_dirty", True), _m("evidence.local", {"tree_dirty": False}),
                     _m("evidence.tree_dirty", False)]),
    _case("smuggled_run_ids_in_evidence_ignored", [TST],
          mutations=[_m(T + "pinned_run_ids.TESTNET_ETHUSDC", "1"),
                     _m("evidence.local", {"pinned_run_ids": {"TESTNET_ETHUSDC": "9004"}}),
                     _m("evidence.pinned_run_ids", {"TESTNET_ETHUSDC": "9004"})]),
    _case("smuggled_git_sha_in_evidence_ignored", [CHK, REV, TST],
          mutations=[_m(T + "git_sha", "e" * 40), _m("evidence.local", {"git_sha": "a" * 40}),
                     _m("evidence.git_sha", "a" * 40)]),
    _case("smuggled_extras_alone_change_nothing", [],
          mutations=[_m("evidence.local", {"commit_author": "x", "tree_dirty": True}),
                     _m("evidence.reviewer_allowlist", []), _m("evidence.tree_dirty", True)]),
    # --- allowlist / trusted-state type checks (fail closed) ----------------------------------
    _case("allowlist_bare_string_rejected", ALL_FAIL,
          mutations=[_m(T + "reviewer_allowlist", "fake-reviewer-alice fake-reviewer-bob fake-reviewer-carol")]),
    _case("allowlist_contains_non_string_rejected", ALL_FAIL,
          mutations=[_m(T + "reviewer_allowlist",
                        ["fake-reviewer-alice", "fake-reviewer-bob", "fake-reviewer-carol", 5])]),
    _case("allowlist_contains_empty_string_rejected", ALL_FAIL,
          mutations=[_m(T + "reviewer_allowlist",
                        ["fake-reviewer-alice", "fake-reviewer-bob", "fake-reviewer-carol", "  "])]),
    _case("allowlist_none_rejected", ALL_FAIL, mutations=[_m(T + "reviewer_allowlist", None)]),
    _case("allowlist_dict_rejected", ALL_FAIL,
          mutations=[_m(T + "reviewer_allowlist", {"fake-reviewer-alice": True})]),
    _case("commit_authors_empty_string_entry_rejected", ALL_FAIL,
          mutations=[_m(T + "commit_authors", ["fake-author-dave", " "])]),
    _case("commit_authors_none_rejected", ALL_FAIL, mutations=[_m(T + "commit_authors", None)]),
    _case("commit_authors_empty_list_rejected", ALL_FAIL, mutations=[_m(T + "commit_authors", [])]),
    _case("commit_authors_bare_string_rejected", ALL_FAIL,
          mutations=[_m(T + "commit_authors", "fake-author-dave")]),
    _case("commit_authors_git_name_not_a_login_rejected", ALL_FAIL,
          mutations=[_m(T + "commit_authors", ["Kan Chang"])]),
    _case("commit_authors_email_rejected", ALL_FAIL,
          mutations=[_m(T + "commit_authors", ["kan@example.com"])]),
    _case("commit_authors_combining_mark_rejected", ALL_FAIL,
          mutations=[_m(T + "commit_authors", ["kotorn\u0301"])]),
    _case("legacy_single_commit_author_key_rejected", ALL_FAIL,
          mutations=[_m(T + "commit_authors", None), _m(T + "commit_author", "fake-author-dave")]),
    _case("allowlist_homoglyph_entry_rejected", ALL_FAIL,
          mutations=[_m(T + "reviewer_allowlist", ["fake-reviewer-alice", "k\u043etorn"])]),
    # --- signer identity must be login-shaped ---------------------------------------------------
    _case("signer_combining_mark_blocks_review", [REV], resign=True,
          signer_override={"REVIEW_ORDER_RISK": "fake-reviewer-bob\u0301"}),
    _case("signer_homoglyph_blocks_review", [REV], resign=True,
          signer_override={"REVIEW_ORDER_RISK": "fake-reviewer-b\u043eb"}),
    _case("signer_line_separator_blocks_review", [REV], resign=True,
          signer_override={"REVIEW_ORDER_RISK": "fake-reviewer-bob\u2028"}),
    _case("signer_underscore_blocks_review", [REV], resign=True,
          signer_override={"REVIEW_ORDER_RISK": "fake_reviewer_bob"}),
    _case("signer_login_too_long_blocks_checks", [CHK], resign=True,
          signer_override={"CHECKS": "a" * 40}),
    _case("verifier_raises", ALL_FAIL, verifier_raises=True),
    _case("verifier_raises_after_checks_verified", ALL_FAIL, verifier_raises=True, verifier_raises_after=1),
    _case("verifier_env_has_no_secrets", [],
          environ={"PATH": "/fake", "BINANCE_API_SECRET": "s3cret", "GITHUB_TOKEN": "tok", "TEMP": "/t"}),
]


def dump() -> str:
    base = build_valid()
    return json.dumps({"now": NOW, "base": base, "cases": CASES}, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(dump(), encoding="utf-8", newline="\n")
