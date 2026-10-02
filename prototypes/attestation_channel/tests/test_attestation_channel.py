import copy
import json
from datetime import UTC, datetime

import pytest

from attestation_channel import core
from attestation_channel.fakes import (CASES, FIXTURE_PATH, apply_case, build_valid, dump, evaluate_doc,
                                       fake_verifier, sign_log)

FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
NOW = datetime.fromisoformat(FIXTURE["now"])
CASE_IDS = [c["name"] for c in FIXTURE["cases"]]
ALL_FAIL = sorted([core.B_RUNTIME, core.B_CHECK, core.B_REVIEW, core.B_TESTNET])


def run_case(case, calls=None):
    doc, log = apply_case(case, FIXTURE["base"])
    raises = RuntimeError("boom") if case.get("verifier_raises") else None
    now = datetime.fromisoformat(case["now"]) if "now" in case else NOW
    verifier = fake_verifier(log, calls, raises, case.get("verifier_raises_after", 0))
    return evaluate_doc(doc, verifier, now=now, environ=case.get("environ"))


def run_doc(doc, log=None, verifier=None, **kwargs):
    verifier = verifier or fake_verifier(log if log is not None else sign_log(doc))
    return evaluate_doc(doc, verifier, now=NOW, **kwargs)


# --- shared-fixture parity (a TS port asserts the same table) ---------------------------------

def test_fixture_file_is_in_sync_with_generator():
    assert FIXTURE_PATH.read_text(encoding="utf-8") == dump()


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=CASE_IDS)
def test_parity_cases(case):
    outcome = run_case(case)
    assert outcome["blockers"] == case["expect_blockers"]
    assert outcome["can_approve"] is case["expect_can_approve"]


def test_every_negative_case_denies_approval_and_positive_set_is_exact():
    positives = [c for c in FIXTURE["cases"] if c["expect_can_approve"]]
    assert all(not c["expect_can_approve"] for c in FIXTURE["cases"] if c["expect_blockers"])
    allowed = {"positive_full_fake_set", "fresh_at_86400s", "future_plus_2s_allowed",
               "free_string_reviewer_in_evidence_is_ignored", "verifier_env_has_no_secrets",
               "allowlist_entries_are_normalized", "co_author_bot_and_unrelated_authors_do_not_block",
               "smuggled_extras_alone_change_nothing",
               "time_accepts_z_suffix", "time_accepts_fraction_z", "time_accepts_nine_digit_fraction",
               "time_accepts_positive_offset", "time_accepts_negative_offset"}
    assert {c["name"] for c in positives} == allowed


# --- explicit negative-first tests -------------------------------------------------------------

def test_no_attestation_keeps_all_three_blockers():
    doc = build_valid()
    doc["evidence"]["attestations"] = {}
    out = run_doc(doc, {})
    assert out["can_approve"] is False
    assert out["blockers"] == sorted([core.B_CHECK, core.B_REVIEW, core.B_TESTNET])


def test_ci_only_attestation_clears_only_check_blocker():
    case = next(c for c in CASES if c["name"] == "ci_only_attestation")
    out = run_case(case)
    assert core.B_CHECK not in out["blockers"]
    assert {core.B_REVIEW, core.B_TESTNET} <= set(out["blockers"])
    assert out["can_approve"] is False


def test_blocker_clears_only_for_its_own_class():
    for dropped, blocker in [("CHECKS", core.B_CHECK), ("TESTNET_ETHUSDC", core.B_TESTNET),
                             ("REVIEW_PERSISTENCE", core.B_REVIEW)]:
        out = run_case({"drop": [dropped]})
        assert out["blockers"] == [blocker]


def test_cross_class_replay_does_not_clear_testnet():
    doc = build_valid()
    log = sign_log(doc)
    atts = doc["evidence"]["attestations"]
    atts["TESTNET_ETHUSDC"] = atts["CHECKS"]
    out = run_doc(doc, log)
    assert core.B_TESTNET in out["blockers"] and out["can_approve"] is False


def test_same_review_attestation_reused_for_all_three_review_slots_fails():
    doc = build_valid()
    log = sign_log(doc)
    atts = doc["evidence"]["attestations"]
    for t in ("REVIEW_ORDER_RISK", "REVIEW_PERSISTENCE"):
        atts[t] = atts["REVIEW_AUTH_RELEASE"]
    out = run_doc(doc, log)
    assert core.B_REVIEW in out["blockers"] and out["can_approve"] is False


def test_verifier_raises_fails_closed_with_runtime_blocker():
    for error in (RuntimeError("x"), TimeoutError(), OSError("gh missing"), ValueError("bad json")):
        out = run_doc(build_valid(), verifier=fake_verifier({}, raises=error))
        assert out["blockers"] == ALL_FAIL and out["can_approve"] is False


def test_partial_verifier_exception_after_checks_verified_closes_everything():
    calls = []
    doc = build_valid()
    verifier = fake_verifier(sign_log(doc), calls, raises=TimeoutError(), raises_after=1)
    out = run_doc(doc, verifier=verifier)
    assert [c["expected"]["evidence_type"] for c in calls][:2] == ["CHECKS", "REVIEW_AUTH_RELEASE"]
    assert out == {"blockers": ALL_FAIL, "can_approve": False}


def test_malformed_evidence_fails_closed_without_raising():
    trusted = build_valid()["trusted"]
    for evidence in (None, [], "x", {"attestations": []}, {"attestations": "x"}):
        out = core.evaluate(evidence, fake_verifier({}), trusted, now=NOW)  # type: ignore[arg-type]
        assert out["can_approve"] is False and core.B_RUNTIME in out["blockers"]


def test_missing_attestations_key_is_just_unattested_not_a_crash():
    out = core.evaluate({}, fake_verifier({}), build_valid()["trusted"], now=NOW)
    assert out == {"blockers": sorted([core.B_CHECK, core.B_REVIEW, core.B_TESTNET]), "can_approve": False}


def test_malformed_trusted_state_fails_closed_without_raising():
    evidence = build_valid()["evidence"]
    for trusted in (None, {}, [], "x", {"commit_author": "a"}, {"commit_authors": ["a"]}):
        out = core.evaluate(evidence, fake_verifier({}), trusted, now=NOW)  # type: ignore[arg-type]
        assert out == {"blockers": ALL_FAIL, "can_approve": False}


def test_trusted_state_is_a_required_argument():
    with pytest.raises(TypeError):
        core.evaluate(build_valid()["evidence"], fake_verifier({}), now=NOW)  # type: ignore[call-arg]


def test_verifier_receives_no_secrets_and_full_expected_binding():
    calls = []
    case = next(c for c in FIXTURE["cases"] if c["name"] == "verifier_env_has_no_secrets")
    out = run_case(case, calls)
    assert out["can_approve"] is True
    assert len(calls) == 5
    atts = FIXTURE["base"]["evidence"]["attestations"]
    for call in calls:
        assert set(call["env"]) <= {"PATH", "TEMP"}
        assert "s3cret" not in json.dumps(call) and "tok" not in call["env"].values()
        expected = call["expected"]
        subject = atts[expected["evidence_type"]]["subject"]
        assert expected["repository_id"] == "1366161771"
        assert expected["git_sha"] == "a" * 40
        assert expected["subject_sha256"] == subject["subject_sha256"]
        assert expected["run_id"] == subject["run_id"]
        assert set(expected) == {"repository", "repository_id", "workflow_ref", "git_sha",
                                 "evidence_type", "subject_sha256", "run_id"}
    workflows = {c["expected"]["evidence_type"]: c["expected"]["workflow_ref"] for c in calls}
    assert workflows["CHECKS"] != workflows["TESTNET_ETHUSDC"] != workflows["REVIEW_ORDER_RISK"]


def test_reviewer_identity_comes_from_signed_claims_not_evidence_string():
    doc = build_valid()
    log = sign_log(doc)
    doc["evidence"]["attestations"]["REVIEW_ORDER_RISK"]["reviewer"] = "fake-reviewer-erin"
    assert run_doc(doc, log)["can_approve"] is True  # the claimed string changes nothing
    doc["trusted"]["commit_authors"] = ["fake-reviewer-bob"]  # the signed identity is the author
    assert run_doc(doc, log)["blockers"] == [core.B_REVIEW]


def test_all_good_gives_no_blockers():
    assert run_doc(build_valid()) == {"blockers": [], "can_approve": True}


# --- defect 1: identity normalization -----------------------------------------------------------

@pytest.mark.parametrize("author", ["fake-reviewer-bob ", " fake-reviewer-bob", "FAKE-REVIEWER-BOB",
                                    "fake-reviewer-bob ", "　fake-reviewer-bob\n",
                                    "ｆａｋｅ-reviewer-bob"])
def test_author_equal_to_reviewer_after_normalization_blocks(author):
    doc = build_valid()
    doc["trusted"]["commit_authors"] = [author]
    assert run_doc(doc)["blockers"] == [core.B_REVIEW]


def test_normalize_identity_contract():
    assert core.normalize_identity("  Ａlice ") == "alice"
    assert core.normalize_identity("ǅ") == core.normalize_identity("ǆ")
    for bad in ("", "   ", "a​b", "a\x00b", None, 3, b"x"):
        with pytest.raises(ValueError):
            core.normalize_identity(bad)


# --- defect 2: trust anchors never come from the evidence blob ----------------------------------

def test_evaluate_ignores_trust_anchors_smuggled_in_evidence():
    doc = build_valid()
    log = sign_log(doc)
    doc["trusted"]["commit_authors"] = ["fake-reviewer-bob"]
    doc["trusted"]["reviewer_allowlist"] = ["fake-reviewer-alice"]
    doc["trusted"]["tree_dirty"] = True
    doc["trusted"]["pinned_run_ids"]["TESTNET_ETHUSDC"] = "1"
    forged = copy.deepcopy(build_valid()["trusted"])
    doc["evidence"].update({"local": forged, **{k: v for k, v in forged.items()}})
    out = run_doc(doc, log)
    assert out["blockers"] == sorted([core.B_REVIEW, core.B_DIRTY, core.B_TESTNET])


def test_evidence_alone_cannot_open_gate_when_trusted_state_disagrees():
    doc = build_valid()
    log = sign_log(doc)
    doc["trusted"]["pinned_run_ids"] = {}
    out = run_doc(doc, log)
    assert out["can_approve"] is False and core.B_CHECK in out["blockers"]


def test_trusted_state_is_not_mutated_or_retained():
    doc = build_valid()
    before = copy.deepcopy(doc)
    run_doc(doc)
    assert doc == before


# --- defect 3: reviewer identity is part of the signed subject ----------------------------------

def _tampering_verifier(doc, mutate):
    inner = fake_verifier(sign_log(doc))

    def verify(statement, bundle, expected, env):
        claims = dict(inner(statement, bundle, expected, env))
        mutate(claims, expected)
        return claims
    return verify


def test_verifier_returning_claims_for_a_different_subject_blocks_that_class():
    doc = build_valid()
    log = sign_log(doc)
    other = log[doc["evidence"]["attestations"]["CHECKS"]["bundle"]]["claims"]

    def verify(statement, bundle, expected, env):
        return other  # CHECKS claims returned for every call
    out = run_doc(doc, verifier=verify)
    assert core.B_REVIEW in out["blockers"] and core.B_TESTNET in out["blockers"]
    assert core.B_CHECK not in out["blockers"] and out["can_approve"] is False


@pytest.mark.parametrize("field", ["subject_sha256", "run_id", "evidence_type", "git_sha", "workflow_ref",
                                   "repository", "repository_id"])
def test_claims_must_match_every_expected_binding(field):
    doc = build_valid()

    def mutate(claims, expected):
        if expected["evidence_type"] == "REVIEW_ORDER_RISK":
            claims[field] = "0" * 8
    out = run_doc(doc, verifier=_tampering_verifier(doc, mutate))
    assert out["blockers"] == [core.B_REVIEW]


def test_claims_missing_expected_binding_or_identity_block():
    for drop in ("repository", "run_id", "subject_sha256", "signer_identity"):
        doc = build_valid()

        def mutate(claims, expected, drop=drop):
            if expected["evidence_type"] == "REVIEW_ORDER_RISK":
                claims.pop(drop)
        out = run_doc(doc, verifier=_tampering_verifier(doc, mutate))
        assert out["blockers"] == [core.B_REVIEW], drop


def test_non_mapping_claims_block_class():
    doc = build_valid()
    out = run_doc(doc, verifier=lambda s, b, e, v: ["not", "claims"])
    assert out["blockers"] == sorted([core.B_CHECK, core.B_REVIEW, core.B_TESTNET])


@pytest.mark.parametrize("identity", ["", "  ", None, 0, 12, ["a"], {"a": 1}, b"alice", True])
def test_empty_or_non_string_signer_identity_blocks_review(identity):
    doc = build_valid()

    def mutate(claims, expected):
        if expected["evidence_type"] == "REVIEW_PERSISTENCE":
            claims["signer_identity"] = identity
    assert run_doc(doc, verifier=_tampering_verifier(doc, mutate))["blockers"] == [core.B_REVIEW]


@pytest.mark.parametrize("variant", ["fake-reviewer-alice", "FAKE-REVIEWER-ALICE", "fake-reviewer-alice  ",
                                     "\tfake-reviewer-alice"])
def test_same_identity_in_two_review_slots_blocks(variant):
    doc = build_valid()

    def mutate(claims, expected):
        if expected["evidence_type"] == "REVIEW_PERSISTENCE":
            claims["signer_identity"] = variant
    assert run_doc(doc, verifier=_tampering_verifier(doc, mutate))["blockers"] == [core.B_REVIEW]


def test_same_identity_in_all_three_slots_blocks():
    doc = build_valid()
    log = sign_log(doc, {k: "fake-reviewer-alice" for k in
                         ("REVIEW_AUTH_RELEASE", "REVIEW_ORDER_RISK", "REVIEW_PERSISTENCE")})
    assert run_doc(doc, log)["blockers"] == [core.B_REVIEW]


# --- defect 4: allowlist types and strict timestamps -------------------------------------------

@pytest.mark.parametrize("allowlist", ["fake-reviewer-alice", None, 5, {"fake-reviewer-alice": 1},
                                       ["fake-reviewer-alice", None], [["x"]], [""], [" "], b"x"])
def test_bad_allowlist_type_fails_closed(allowlist):
    doc = build_valid()
    doc["trusted"]["reviewer_allowlist"] = allowlist
    assert run_doc(doc) == {"blockers": ALL_FAIL, "can_approve": False}


@pytest.mark.parametrize("container", [list, tuple, set, frozenset])
def test_allowlist_accepts_list_set_tuple(container):
    doc = build_valid()
    doc["trusted"]["reviewer_allowlist"] = container(doc["trusted"]["reviewer_allowlist"])
    assert run_doc(doc) == {"blockers": [], "can_approve": True}


@pytest.mark.parametrize("value,ok", [
    ("2026-10-02T11:00:00Z", True), ("2026-10-02T11:00:00.5+07:00", True),
    ("2026-10-02T11:00:00-00:00", True),
    ("2026-10-02T11:00:00", False), ("20261002T110000Z", False), ("2026-10-02T11:00:00z", False),
    ("2026-10-02 11:00:00Z", False), ("2026-10-02T11:00:00+0000", False), ("", False), (None, False)])
def test_parse_time_strict_rfc3339(value, ok):
    if ok:
        assert core.parse_rfc3339(value).tzinfo is not None
    else:
        with pytest.raises(ValueError):
            core.parse_rfc3339(value)


def test_parse_time_converts_offsets_exactly():
    assert core.parse_rfc3339("2026-10-02T18:00:00+07:00") == datetime(2026, 10, 2, 11, tzinfo=UTC)


# --- D1: the verifier cannot rewrite what the gate compares against -----------------------------

def test_verifier_mutating_expected_cannot_launder_forged_claims():
    doc = build_valid()
    inner = fake_verifier(sign_log(doc))

    def verify(statement, bundle, expected, env):
        claims = dict(inner(statement, bundle, expected, env))
        if expected["evidence_type"] == "REVIEW_ORDER_RISK":
            expected.update({"repository": "evil"})  # edit the gate's own expectation...
            claims["repository"] = "evil"            # ...and return claims that now "match" it
        return claims
    assert run_doc(doc, verifier=verify)["blockers"] == [core.B_REVIEW]


@pytest.mark.parametrize("field", ["repository", "repository_id", "workflow_ref", "git_sha",
                                   "evidence_type", "subject_sha256", "run_id"])
def test_verifier_mutating_any_expected_field_blocks_its_class(field):
    doc = build_valid()
    inner = fake_verifier(sign_log(doc))

    def verify(statement, bundle, expected, env):
        claims = dict(inner(statement, bundle, expected, env))
        if expected["evidence_type"] == "CHECKS":
            expected[field] = "forged"
            claims[field] = "forged"
        return claims
    assert run_doc(doc, verifier=verify)["blockers"] == [core.B_CHECK]


def test_verifier_mutating_live_subject_in_evidence_is_compared_against_snapshot():
    doc = build_valid()
    inner = fake_verifier(sign_log(doc))
    live = doc["evidence"]["attestations"]["TESTNET_ETHUSDC"]["subject"]

    def verify(statement, bundle, expected, env):
        claims = dict(inner(statement, bundle, expected, env))
        if expected["evidence_type"] == "TESTNET_ETHUSDC":
            live["policy_sha256"] = "0" * 64   # rewrite the live subject after the fact
            claims["policy_sha256"] = "0" * 64
        return claims
    assert run_doc(doc, verifier=verify)["blockers"] == [core.B_TESTNET]


def test_verifier_mutating_nested_expected_copy_does_not_leak_between_calls():
    doc = build_valid()
    inner = fake_verifier(sign_log(doc))
    seen = []

    def verify(statement, bundle, expected, env):
        seen.append(dict(expected))
        expected["repository"] = "evil"  # pollution must not reach the next call
        return inner(statement, bundle, {**expected, "repository": core.REPOSITORY}, env)
    run_doc(doc, verifier=verify)
    assert all(s["repository"] == core.REPOSITORY for s in seen)


# --- D2: identities must be GitHub-login shaped (ASCII) -----------------------------------------

@pytest.mark.parametrize("bad", ["kotorn\u0301", "k\u043etorn", "kotorn\u2028", "ko\u2028torn", "ko\u2029torn",
                                 "ko torn", "ko_torn", "-kotorn", "kotorn-", "a" * 40, "kotorn[bot]x",
                                 "kotorn[bot", "[bot]", "kotorn[bot][bot]", "kotorn\u200b", "ko\ttorn",
                                 "k\u00f6torn", "kotorn@x.com", "a--" + "b" * 40])
def test_normalize_login_rejects_non_login_shapes(bad):
    with pytest.raises(ValueError):
        core.normalize_login(bad)


@pytest.mark.parametrize("good,canon", [("kotorn", "kotorn"), (" KoTorn ", "kotorn"), ("a", "a"),
                                        ("a" * 39, "a" * 39), ("a-b", "a-b"), ("a--b", "a--b"),
                                        ("Dependabot[BOT]", "dependabot[bot]"), ("ｋｏｔｏｒｎ", "kotorn"),
                                        ("9x", "9x")])
def test_normalize_login_accepts_and_canonicalizes(good, canon):
    assert core.normalize_login(good) == canon


@pytest.mark.parametrize("identity", ["kotorn\u0301", "k\u043etorn", "kotorn\u2028", "ko\u2028torn",
                                      "ko_torn", "a" * 40, "-x"])
def test_invalid_signer_login_shape_blocks_review_class(identity):
    doc = build_valid()

    def mutate(claims, expected):
        if expected["evidence_type"] == "REVIEW_ORDER_RISK":
            claims["signer_identity"] = identity
    assert run_doc(doc, verifier=_tampering_verifier(doc, mutate))["blockers"] == [core.B_REVIEW]


def test_invalid_signer_login_shape_on_checks_blocks_only_checks():
    doc = build_valid()

    def mutate(claims, expected):
        if expected["evidence_type"] == "CHECKS":
            claims["signer_identity"] = "kotorn\u0301"
    assert run_doc(doc, verifier=_tampering_verifier(doc, mutate))["blockers"] == [core.B_CHECK]


def test_combining_mark_reviewer_cannot_impersonate_commit_author_by_visual_match():
    # "kotorn" + U+0301 renders like a different login; it must be rejected, not treated as a new person.
    doc = build_valid()
    log = sign_log(doc, {"REVIEW_ORDER_RISK": "kotorn\u0301"})
    doc["trusted"]["reviewer_allowlist"] = doc["trusted"]["reviewer_allowlist"] + ["kotorn"]
    assert run_doc(doc, log)["blockers"] == [core.B_REVIEW]


@pytest.mark.parametrize("bad", ["kotorn\u0301", "k\u043etorn", "kotorn\u2028x", "bad_name"])
def test_invalid_login_in_trusted_allowlist_fails_closed(bad):
    doc = build_valid()
    doc["trusted"]["reviewer_allowlist"] = doc["trusted"]["reviewer_allowlist"] + [bad]
    assert run_doc(doc) == {"blockers": ALL_FAIL, "can_approve": False}


# --- D4: commit_authors covers every author/committer/co-author in the reviewed range -----------

@pytest.mark.parametrize("who", ["fake-reviewer-alice", "fake-reviewer-bob", "fake-reviewer-carol"])
def test_any_listed_commit_author_blocks_a_matching_reviewer(who):
    doc = build_valid()
    doc["trusted"]["commit_authors"] = ["fake-author-dave", "fake-author-zed", who.upper()]
    assert run_doc(doc)["blockers"] == [core.B_REVIEW]


def test_two_reviewers_among_authors_still_blocks():
    doc = build_valid()
    doc["trusted"]["commit_authors"] = ["fake-reviewer-alice", "fake-reviewer-carol"]
    assert run_doc(doc)["blockers"] == [core.B_REVIEW]


def test_unrelated_multiple_authors_do_not_block():
    doc = build_valid()
    doc["trusted"]["commit_authors"] = ["fake-author-dave", "fake-author-zed", "dependabot[bot]"]
    assert run_doc(doc) == {"blockers": [], "can_approve": True}


@pytest.mark.parametrize("authors", [None, [], (), set(), "fake-author-dave", {"fake-author-dave": 1}, 5,
                                     ["fake-author-dave", None], ["fake-author-dave", ""],
                                     ["fake-author-dave", "Kan Chang"], ["fake-author-dave", "a@b.co"],
                                     ["kotorń"], ["kоtorn"], [b"x"]])
def test_bad_commit_authors_fail_closed_globally(authors):
    doc = build_valid()
    doc["trusted"]["commit_authors"] = authors
    assert run_doc(doc) == {"blockers": ALL_FAIL, "can_approve": False}


def test_legacy_single_commit_author_key_is_not_accepted():
    doc = build_valid()
    del doc["trusted"]["commit_authors"]
    doc["trusted"]["commit_author"] = "fake-author-dave"
    assert run_doc(doc) == {"blockers": ALL_FAIL, "can_approve": False}


@pytest.mark.parametrize("container", [list, tuple, set, frozenset])
def test_commit_authors_accepts_list_set_tuple(container):
    doc = build_valid()
    doc["trusted"]["commit_authors"] = container(doc["trusted"]["commit_authors"])
    assert run_doc(doc) == {"blockers": [], "can_approve": True}


# --- D3: success path never emits the runtime blocker ---------------------------------------------

def test_success_path_has_no_runtime_blocker_and_can_approve_tracks_blockers_only():
    out = run_doc(build_valid())
    assert core.B_RUNTIME not in out["blockers"] and out["can_approve"] is (not out["blockers"])
    dirty = build_valid()
    dirty["trusted"]["tree_dirty"] = True
    out = run_doc(dirty)
    assert out["blockers"] == [core.B_DIRTY] and core.B_RUNTIME not in out["blockers"]
