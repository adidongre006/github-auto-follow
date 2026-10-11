"""Eligibility rules and their explanations."""

from src.discovery import Candidate
from src.eligibility import EligibilityCriteria, check_candidate, check_profile
from tests.helpers import NOW, make_profile

CRITERIA = EligibilityCriteria(account_types=frozenset({"user"}), excluded_users=frozenset({"spammer"}),
                               min_account_age_days=30)


def test_normal_user_passes_stage_one():
    assert check_candidate(Candidate("alice", 1, "User"), CRITERIA).eligible


def test_excluded_username_is_rejected_case_insensitively():
    decision = check_candidate(Candidate("SPAMMER", 2, "User"), CRITERIA)
    assert not decision.eligible and "exclusion" in decision.reason


def test_organizations_are_rejected_by_default():
    decision = check_candidate(Candidate("acme", 3, "Organization"), CRITERIA)
    assert not decision.eligible and "Organization" in decision.reason


def test_allowed_types_are_configurable():
    criteria = EligibilityCriteria(frozenset({"user", "organization"}), frozenset(), 0)
    assert check_candidate(Candidate("acme", 3, "Organization"), criteria).eligible


def test_profile_type_is_rechecked_on_the_real_profile():
    assert not check_profile(make_profile("x", kind="Bot"), CRITERIA, NOW).eligible


def test_young_account_is_rejected_with_explanation():
    decision = check_profile(make_profile("new", created="2026-10-01T00:00:00Z"), CRITERIA, NOW)
    assert not decision.eligible and "9 days old" in decision.reason


def test_old_enough_account_passes():
    assert check_profile(make_profile("old", created="2020-05-05T05:05:05Z"), CRITERIA, NOW).eligible


def test_age_rule_is_skipped_when_created_at_is_missing_or_invalid():
    for created in (None, "not-a-date", ""):
        profile = make_profile("x")
        profile["created_at"] = created
        assert check_profile(profile, CRITERIA, NOW).eligible


def test_age_rule_can_be_disabled():
    criteria = EligibilityCriteria(frozenset({"user"}), frozenset(), 0)
    assert check_profile(make_profile("new", created="2026-10-10T00:00:00Z"), criteria, NOW).eligible
